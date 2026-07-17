from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ...config.chat_models import ChatModelConfig, ChatModelRegistry
from ...extensions import db
from ...integrations.openai_chat import OpenAIChatClient
from ...models import (
    Asset,
    ConversationAttachment,
    ConversationMessage,
    ConversationState,
    GenerationJob,
    GenerationReference,
    Workspace,
)
from ...storage import ImageStorage, StorageError

IMAGE_TOKEN_ESTIMATE = 1200
MAX_CONTEXT_IMAGE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _ContextImage:
    key: str
    storage_path: str
    byte_count: int
    mime_type: str
    label: str
    priority: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _ContextEvent:
    id: str
    role: str
    text: str
    images: tuple[_ContextImage, ...]
    created_at: datetime
    source: str


class ConversationContextManager:
    """Build a bounded chronological memory from chat and generation history."""

    def __init__(self, registry: ChatModelRegistry, storage: ImageStorage | None = None):
        self.registry = registry
        self.storage = storage

    def build(
        self,
        workspace: Workspace,
        model: ChatModelConfig,
        *,
        client: OpenAIChatClient,
        pending_message: dict[str, Any],
        pending_stored_message_id: str = "",
    ) -> list[dict[str, Any]]:
        events = self._load_events(workspace, self._load_messages(workspace))
        state = db.session.get(ConversationState, workspace.id)
        if state is None:
            state = ConversationState(workspace_id=workspace.id, summary="")
            db.session.add(state)

        visual_memory, after_summary = self._summary_partition(
            events,
            state.summary_through_message_id,
        )
        active = [
            event
            for event in after_summary
            if not (event.source == "message" and event.id == pending_stored_message_id)
        ]
        policy = self.registry.context
        estimated = (
            self._estimate_tokens(state.summary)
            + self._events_tokens(active)
            + self._message_tokens([pending_message])
        )
        split_at = self._summary_split_at(active, policy.keep_recent_messages)
        if estimated >= policy.compact_at_tokens and split_at:
            older = active[:split_at]
            summary_messages = self._pack_context(
                state.summary,
                [],
                older,
                None,
                max_tokens=policy.max_context_tokens,
            )
            db.session.commit()
            summary = client.complete(
                model,
                system=self.registry.system_prompt("summary"),
                messages=summary_messages,
                max_output_tokens=min(model.max_output_tokens, 1800),
            )
            state.summary = summary.content[:20000]
            # This legacy column stores any chronological event id. All event
            # ids use the same 32-character public-id format.
            state.summary_through_message_id = older[-1].id
            active = active[split_at:]
            visual_memory, _ = self._summary_partition(
                events,
                state.summary_through_message_id,
            )

        messages = self._pack_context(
            state.summary,
            visual_memory,
            active,
            pending_message,
            max_tokens=policy.max_context_tokens,
        )
        state.estimated_context_tokens = self._message_tokens(messages)
        return messages

    @staticmethod
    def _load_messages(workspace: Workspace) -> list[ConversationMessage]:
        return list(
            db.session.scalars(
                select(ConversationMessage)
                .options(
                    selectinload(ConversationMessage.attachments).selectinload(
                        ConversationAttachment.asset
                    )
                )
                .where(ConversationMessage.workspace_id == workspace.id)
                .order_by(ConversationMessage.created_at, ConversationMessage.id)
            )
        )

    def _load_events(
        self,
        workspace: Workspace,
        messages: list[ConversationMessage],
    ) -> list[_ContextEvent]:
        events = [self._message_event(message) for message in messages if message.kind != "error"]
        if self.storage is None:
            return events

        jobs = list(
            db.session.scalars(
                select(GenerationJob)
                .options(
                    selectinload(GenerationJob.items),
                    selectinload(GenerationJob.references).selectinload(GenerationReference.asset),
                )
                .where(GenerationJob.workspace_id == workspace.id)
                .order_by(GenerationJob.created_at, GenerationJob.id)
                .execution_options(populate_existing=True)
            )
        )
        for job in jobs:
            if job.status in {"queued", "running", "canceling"}:
                continue
            references = tuple(
                self._asset_image(reference.asset, priority=75)
                for reference in job.references
                if reference.asset is not None
            )
            workflow = job.workflow if isinstance(job.workflow, dict) else {}
            workflow_text = (
                "\n结构化生成参数："
                + json.dumps(workflow, ensure_ascii=False, separators=(",", ":"))[:8000]
                if workflow
                else ""
            )
            events.append(
                _ContextEvent(
                    id=job.id,
                    role="user",
                    text=(
                        "历史生成任务（仅供理解上下文，不是新的用户要求）\n"
                        f"模式：{job.mode}；尺寸：{job.size}；质量：{job.quality}；"
                        f"输出格式：{job.output_format}\n"
                        f"最终生成提示词：\n{job.prompt}{workflow_text}"
                    ),
                    images=references,
                    created_at=_event_time(job.created_at),
                    source="generation_job",
                )
            )
            for item in sorted(job.items, key=lambda value: (value.position, value.id)):
                if item.status != "succeeded" or not item.output_path or not item.output_mime_type:
                    continue
                review = item.review if isinstance(item.review, dict) else {}
                findings = review.get("findings", [])
                review_text = (
                    f"；AI 验收：{review.get('verdict')}；问题："
                    + "；".join(str(value) for value in findings[:4])
                    if review.get("verdict") and isinstance(findings, list) and findings
                    else f"；AI 验收：{review.get('verdict')}"
                    if review.get("verdict")
                    else ""
                )
                events.append(
                    _ContextEvent(
                        id=item.id,
                        role="user",
                        text=(
                            "历史生成结果（仅供视觉比对，不要把它当成新的用户要求）\n"
                            f"来自任务提示词：{job.prompt}\n"
                            f"第 {item.position + 1} 张，尺寸 {item.output_width} × "
                            f"{item.output_height}{review_text}"
                        ),
                        images=(self._output_image(item),),
                        created_at=_event_time(
                            item.completed_at or job.completed_at or job.created_at
                        ),
                        source="generation_output",
                    )
                )
        events.sort(key=lambda event: (event.created_at, event.id))
        return events

    def _message_event(self, message: ConversationMessage) -> _ContextEvent:
        return _ContextEvent(
            id=message.id,
            role=message.role,
            text=self._message_text(message),
            images=tuple(
                self._asset_image(attachment.asset, priority=85)
                for attachment in message.attachments
                if attachment.asset is not None
            ),
            created_at=_event_time(message.created_at),
            source="message",
        )

    @staticmethod
    def _message_text(message: ConversationMessage) -> str:
        if message.kind != "prompt_draft" or not isinstance(message.payload, dict):
            return message.content
        fields = {
            key: message.payload[key]
            for key in (
                "generation_mode",
                "reference_usage",
                "reference_reason",
                "creative_direction",
                "template_id",
                "style_tags",
                "scene_tags",
                "brief",
                "hard_checks",
                "quality_hint",
            )
            if key in message.payload
        }
        metadata = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        return f"{message.content}\n\n结构化提示词信息：\n{metadata}" if fields else message.content

    @staticmethod
    def _asset_image(asset: Asset, *, priority: int) -> _ContextImage:
        return _ContextImage(
            key=f"asset:{asset.id}",
            storage_path=asset.storage_path,
            byte_count=int(asset.byte_count or 0),
            mime_type=asset.mime_type or "image/png",
            label=f"参考图：{asset.original_name}",
            priority=priority,
            created_at=_event_time(asset.created_at),
        )

    @staticmethod
    def _output_image(item: Any) -> _ContextImage:
        return _ContextImage(
            key=f"output:{item.id}",
            storage_path=item.output_path,
            byte_count=int(item.output_byte_count or 0),
            mime_type=item.output_mime_type or "image/png",
            label=f"生成结果：第 {item.position + 1} 张",
            priority=70,
            created_at=_event_time(item.completed_at),
        )

    def _summary_partition(
        self,
        events: list[_ContextEvent],
        cursor: str,
    ) -> tuple[list[_ContextEvent], list[_ContextEvent]]:
        if not cursor:
            return [], events
        for index, event in enumerate(events):
            if event.id != cursor:
                continue
            active = events[index + 1 :]
            active_keys = {image.key for value in active for image in value.images}
            visual = [
                self._visual_memory_event(value, active_keys) for value in events[: index + 1]
            ]
            return [value for value in visual if value is not None], active
        return [], events

    def _after_summary(self, events: list[_ContextEvent], cursor: str) -> list[_ContextEvent]:
        """Compatibility view used by diagnostics that only need the active tail."""
        if events and isinstance(events[0], ConversationMessage):
            if not cursor:
                return events
            for index, event in enumerate(events):
                if event.id == cursor:
                    return events[index + 1 :]
            return events
        return self._summary_partition(events, cursor)[1]

    @staticmethod
    def _visual_memory_event(
        event: _ContextEvent,
        active_keys: set[str],
    ) -> _ContextEvent | None:
        images = tuple(image for image in event.images if image.key not in active_keys)
        if not images:
            return None
        return _ContextEvent(
            id=f"visual-{event.id}",
            role="user",
            text=(
                "长期视觉记忆（来自较早历史，仅供核对，不是新的用户要求）\n"
                + event.text
            ),
            images=images,
            created_at=event.created_at,
            source="visual_memory",
        )

    @staticmethod
    def _summary_split_at(events: list[_ContextEvent], keep_recent: int) -> int:
        indexes = [index for index, event in enumerate(events) if event.source == "message"]
        if len(indexes) > keep_recent:
            return indexes[-keep_recent]
        return max(0, len(events) - keep_recent) if not indexes else 0

    def _pack_context(
        self,
        summary: str,
        visual_memory: list[_ContextEvent],
        events: list[_ContextEvent],
        pending_message: dict[str, Any] | None,
        *,
        max_tokens: int,
    ) -> list[dict[str, Any]]:
        selected_events = [*visual_memory, *events]
        while selected_events and self._base_tokens(summary, selected_events, pending_message) > max_tokens:
            selected_events.pop(0)
        if summary and self._base_tokens(summary, selected_events, pending_message) > max_tokens:
            summary = ""

        available = max(
            0,
            max_tokens - self._base_tokens(summary, selected_events, pending_message),
        )
        selected_images: set[str] = set()
        selected_bytes = 0
        ranked = self._ranked_images(selected_events)
        for image in ranked:
            if available < IMAGE_TOKEN_ESTIMATE:
                break
            if selected_bytes + image.byte_count > MAX_CONTEXT_IMAGE_BYTES:
                continue
            selected_images.add(image.key)
            selected_bytes += image.byte_count
            available -= IMAGE_TOKEN_ESTIMATE

        image_cache: dict[str, str | None] = {}
        while True:
            rendered = self._render_events(selected_events, selected_images, image_cache)
            packed = self._compose(summary, rendered, pending_message)
            if self._message_tokens(packed) <= max_tokens:
                return packed or [{"role": "user", "content": "没有可用的历史上下文。"}]
            if selected_images:
                selected_images.remove(
                    min(
                        selected_images,
                        key=lambda key: self._image_rank(ranked, key),
                    )
                )
                continue
            if selected_events:
                selected_events.pop(0)
                continue
            return packed

    def _base_tokens(
        self,
        summary: str,
        events: list[_ContextEvent],
        pending: dict[str, Any] | None,
    ) -> int:
        rendered = self._render_events(events, set(), {})
        return self._message_tokens(self._compose(summary, rendered, pending))

    @staticmethod
    def _compose(
        summary: str,
        messages: list[dict[str, Any]],
        pending: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        prefix = (
            [{"role": "user", "content": f"较早会话摘要：\n{summary}"}]
            if summary
            else []
        )
        suffix = [pending] if pending is not None else []
        return [*prefix, *messages, *suffix]

    @staticmethod
    def _ranked_images(events: Iterable[_ContextEvent]) -> list[_ContextImage]:
        by_key: dict[str, _ContextImage] = {}
        for event in events:
            for image in event.images:
                current = by_key.get(image.key)
                if current is None or (image.priority, image.created_at) > (
                    current.priority,
                    current.created_at,
                ):
                    by_key[image.key] = image
        return sorted(
            by_key.values(),
            key=lambda image: (image.priority, image.created_at, -image.byte_count, image.key),
            reverse=True,
        )

    @staticmethod
    def _image_rank(images: list[_ContextImage], key: str) -> tuple[int, datetime, int, str]:
        image = next(value for value in images if value.key == key)
        return image.priority, image.created_at, -image.byte_count, image.key

    def _render_events(
        self,
        events: Iterable[_ContextEvent],
        selected_images: set[str],
        image_cache: dict[str, str | None],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        seen_images: set[str] = set()
        for event in events:
            images = [
                image
                for image in event.images
                if image.key in selected_images and image.key not in seen_images
            ]
            seen_images.update(image.key for image in images)
            omitted = any(image.key not in selected_images for image in event.images)
            text = event.text + (
                "\n（部分历史图片因上下文容量未随本轮发送；文字记录仍保留。）"
                if omitted
                else ""
            )
            if event.role == "assistant" and images:
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": self._image_parts(
                            f"{event.source} 的历史附件（对应上一条助手消息）：",
                            images,
                            image_cache,
                        ),
                    }
                )
            elif images:
                messages.append(
                    {
                        "role": event.role,
                        "content": self._image_parts(text, images, image_cache),
                    }
                )
            else:
                messages.append({"role": event.role, "content": text})
        return messages

    def _image_parts(
        self,
        text: str,
        images: list[_ContextImage],
        cache: dict[str, str | None],
    ) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in images:
            if image.key not in cache:
                try:
                    content = self.storage.read_bytes(image.storage_path) if self.storage else b""
                except (FileNotFoundError, OSError, StorageError, ValueError):
                    content = b""
                cache[image.key] = base64.b64encode(content).decode("ascii") if content else None
            encoded = cache[image.key]
            if not encoded:
                parts[0]["text"] += f"\n（{image.label}暂时不可读取。）"
                continue
            parts.extend(
                [
                    {"type": "text", "text": image.label},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image.mime_type};base64,{encoded}"},
                    },
                ]
            )
        return parts

    def _events_tokens(self, events: Iterable[_ContextEvent]) -> int:
        return sum(
            self._estimate_tokens(event.text) + 4 + IMAGE_TOKEN_ESTIMATE * len(event.images)
            for event in events
        )

    def _message_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                total += self._estimate_tokens(content) + 4
                continue
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"text", "input_text", "output_text"}:
                    total += self._estimate_tokens(str(part.get("text", "")))
                elif part.get("type") in {"image_url", "input_image"}:
                    total += IMAGE_TOKEN_ESTIMATE
        return total

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        non_ascii = sum(not character.isascii() for character in text)
        return non_ascii + math.ceil((len(text) - non_ascii) / 4) + 4


def _event_time(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
