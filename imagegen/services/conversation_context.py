from __future__ import annotations

import math
from typing import Any

from sqlalchemy import select

from ..config.chat_models import ChatModelConfig, ChatModelRegistry
from ..extensions import db
from ..integrations.openai_chat import OpenAIChatClient
from ..models import ConversationMessage, ConversationState, Workspace


class ConversationContextManager:
    """在保留完整持久化会话的同时构建有长度上限的模型上下文。"""

    def __init__(self, registry: ChatModelRegistry):
        self.registry = registry

    def build(
        self,
        workspace: Workspace,
        model: ChatModelConfig,
        *,
        client: OpenAIChatClient,
        pending_message: dict[str, Any],
        pending_stored_message_id: str = "",
    ) -> list[dict[str, Any]]:
        stored = list(
            db.session.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.workspace_id == workspace.id)
                .order_by(ConversationMessage.created_at, ConversationMessage.id)
            )
        )
        state = db.session.get(ConversationState, workspace.id)
        if state is None:
            state = ConversationState(workspace_id=workspace.id, summary="")
            db.session.add(state)
        active = [
            message
            for message in self._after_summary(stored, state.summary_through_message_id)
            if message.kind != "error" and message.id != pending_stored_message_id
        ]
        policy = self.registry.context
        estimated = (
            self._estimate_tokens(state.summary)
            + sum(self._estimate_tokens(message.content) for message in active)
            + self._message_tokens([pending_message])
        )
        if estimated >= policy.compact_at_tokens and len(active) > policy.keep_recent_messages:
            older = active[: -policy.keep_recent_messages]
            summary_input = self._summary_input(state.summary, older)
            db.session.commit()
            summary = client.complete(
                model,
                system=self.registry.system_prompt("summary"),
                messages=[{"role": "user", "content": summary_input}],
                max_output_tokens=min(model.max_output_tokens, 1800),
            )
            state.summary = summary.content[:20000]
            state.summary_through_message_id = older[-1].id
            active = active[-policy.keep_recent_messages :]

        messages: list[dict[str, Any]] = [
            {"role": message.role, "content": message.content} for message in active
        ]
        if state.summary:
            messages.insert(0, {"role": "user", "content": f"较早会话摘要：\n{state.summary}"})
        messages.append(pending_message)
        while len(messages) > 5 and self._message_tokens(messages) > policy.max_context_tokens:
            messages.pop(1 if state.summary else 0)
        state.estimated_context_tokens = self._message_tokens(messages)
        return messages

    @staticmethod
    def _after_summary(
        messages: list[ConversationMessage], summary_through_message_id: str
    ) -> list[ConversationMessage]:
        if not summary_through_message_id:
            return messages
        for index, message in enumerate(messages):
            if message.id == summary_through_message_id:
                return messages[index + 1 :]
        return messages

    @staticmethod
    def _summary_input(summary: str, messages: list[ConversationMessage]) -> str:
        parts = []
        if summary:
            parts.append(f"已有摘要：\n{summary}")
        transcript = "\n\n".join(
            f"{'用户' if message.role == 'user' else '助手'}：{message.content}"
            for message in messages
        )
        parts.append(f"需要合并的较早对话：\n{transcript}")
        return "\n\n".join(parts)

    def _message_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                total += self._estimate_tokens(content) + 4
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        total += self._estimate_tokens(str(part.get("text", "")))
                    elif part.get("type") == "image_url":
                        total += 1200
        return total

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        non_ascii = sum(not character.isascii() for character in text)
        return non_ascii + math.ceil((len(text) - non_ascii) / 4) + 4
