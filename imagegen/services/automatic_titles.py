from __future__ import annotations

import re
from concurrent.futures import Executor, ThreadPoolExecutor
from threading import Lock
from typing import Any

from flask import Flask
from sqlalchemy import func, or_, select, update

from ..config.chat_models import ChatModelRegistry
from ..extensions import db
from ..integrations.openai_chat import OpenAIChatClient, OpenAIChatError
from ..models import ConversationMessage, Workspace, utcnow

AUTO_TITLE_SYSTEM_PROMPT = """根据用户的第一条消息，为视觉创作工作站生成一个简短、具体的标题。
概括核心创作对象、动作或用途，不要补充用户没有提供的信息。
只输出标题本身，不要引号、句号、前缀、解释或换行，最多 36 个字符。"""
AUTO_TITLE_MAX_LENGTH = 36


class AutomaticTitleService:
    """异步生成并安全应用工作站自动标题。"""

    def __init__(
        self,
        chat_models: ChatModelRegistry,
        *,
        app: Flask,
        client: OpenAIChatClient | None = None,
        executor: Executor | None = None,
    ):
        self.chat_models = chat_models
        self.client = client or OpenAIChatClient()
        self._app = app
        self._executor = executor
        self._executor_lock = Lock()
        self._closed = False

    def schedule(
        self,
        *,
        workspace_id: str,
        message_id: str,
        expected_title: str,
        content: str,
        model_id: str,
    ) -> None:
        try:
            self._get_executor().submit(
                self._generate,
                workspace_id,
                message_id,
                expected_title,
                content,
                model_id,
            )
        except RuntimeError:
            self._app.logger.warning(
                "无法安排工作站自动标题任务：%s",
                workspace_id,
            )

    def close(self) -> None:
        with self._executor_lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    def _get_executor(self) -> Executor:
        with self._executor_lock:
            if self._closed:
                raise RuntimeError("自动标题服务已关闭")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=2,
                    thread_name_prefix="workspace-title",
                )
            return self._executor

    def _generate(
        self,
        workspace_id: str,
        message_id: str,
        expected_title: str,
        content: str,
        model_id: str,
    ) -> None:
        with self._app.app_context():
            try:
                if not self._eligible(workspace_id, message_id, expected_title):
                    return
                db.session.remove()
                model = self.chat_models.get(model_id)
                result = self.client.complete(
                    model,
                    system=AUTO_TITLE_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": content}],
                    max_output_tokens=min(model.max_output_tokens, 128),
                )
                title = self._normalize(result.content)
                if title:
                    self._apply(workspace_id, message_id, expected_title, title)
            except OpenAIChatError as error:
                db.session.rollback()
                self._app.logger.warning(
                    "工作站自动标题失败（%s）：%s",
                    workspace_id,
                    error,
                )
            except Exception:
                db.session.rollback()
                self._app.logger.exception(
                    "工作站自动标题失败：%s",
                    workspace_id,
                )
            finally:
                db.session.remove()

    @staticmethod
    def _filters(
        workspace_id: str,
        message_id: str,
        expected_title: str,
    ) -> tuple[Any, ...]:
        auto_title = Workspace.settings["auto_title"].as_boolean()
        message_exists = (
            select(ConversationMessage.id)
            .where(
                ConversationMessage.id == message_id,
                ConversationMessage.workspace_id == workspace_id,
                ConversationMessage.role == "user",
            )
            .exists()
        )
        return (
            Workspace.id == workspace_id,
            Workspace.name == expected_title,
            or_(auto_title.is_(True), auto_title.is_(None)),
            message_exists,
        )

    def _eligible(self, workspace_id: str, message_id: str, expected_title: str) -> bool:
        return bool(
            db.session.scalar(
                select(Workspace.id)
                .where(*self._filters(workspace_id, message_id, expected_title))
                .limit(1)
            )
        )

    def _apply(
        self,
        workspace_id: str,
        message_id: str,
        expected_title: str,
        title: str,
    ) -> None:
        user_id = db.session.scalar(
            select(Workspace.user_id).where(
                *self._filters(workspace_id, message_id, expected_title)
            )
        )
        if user_id is None:
            db.session.rollback()
            return
        candidate = self._unique_title(int(user_id), workspace_id, title)
        db.session.execute(
            update(Workspace)
            .where(*self._filters(workspace_id, message_id, expected_title))
            .values(name=candidate, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        db.session.commit()

    @staticmethod
    def _normalize(content: str) -> str:
        first_line = next((line.strip() for line in str(content).splitlines() if line.strip()), "")
        title = re.sub(r"^(?:工作站)?标题\s*[:：]\s*", "", first_line, flags=re.IGNORECASE)
        title = title.strip(" `*_#\"'“”‘’")
        title = re.sub(r"\s+", " ", title).strip()
        return title[:AUTO_TITLE_MAX_LENGTH].rstrip("，。！？,.!?；;：:")

    @staticmethod
    def _unique_title(user_id: int, workspace_id: str, title: str) -> str:
        base = title[:AUTO_TITLE_MAX_LENGTH]
        candidate = base
        suffix = 2
        while db.session.scalar(
            select(Workspace.id)
            .where(
                Workspace.user_id == user_id,
                Workspace.id != workspace_id,
                func.lower(Workspace.name) == candidate.lower(),
            )
            .limit(1)
        ):
            marker = f" {suffix}"
            candidate = f"{base[: AUTO_TITLE_MAX_LENGTH - len(marker)]}{marker}"
            suffix += 1
        return candidate
