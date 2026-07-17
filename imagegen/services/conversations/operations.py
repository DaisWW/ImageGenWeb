from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any

from ...errors import ServiceError
from ...models import Workspace, utcnow
from ..settings import SystemSettingsService


@dataclass(frozen=True)
class ConversationOperation:
    user_id: int
    kind: str
    label: str
    started_at: datetime

    def public_dict(self) -> dict[str, Any]:
        return {
            "busy": True,
            "kind": self.kind,
            "label": self.label,
            "started_at": self.started_at.isoformat(),
        }


class ConversationOperationRegistry:
    def __init__(self, settings: SystemSettingsService):
        self.settings = settings
        self._operation_lock = Lock()
        self._operations: dict[str, ConversationOperation] = {}

    def state(self, workspace_id: str) -> dict[str, Any]:
        with self._operation_lock:
            operation = self._operations.get(workspace_id)
        if operation is None:
            return {"busy": False, "kind": "", "label": "", "started_at": None}
        return operation.public_dict()

    @contextmanager
    def generation_submission(self, workspace: Workspace) -> Iterator[None]:
        with self.workspace_operation(
            workspace,
            "generation_submission",
            "正在提交生成任务",
            enforce_chat_capacity=False,
        ):
            yield

    @contextmanager
    def workspace_mutation(self, workspace: Workspace, label: str) -> Iterator[None]:
        with self.workspace_operation(
            workspace,
            "workspace_mutation",
            label,
            enforce_chat_capacity=False,
        ):
            yield

    @contextmanager
    def workspace_operation(
        self,
        workspace: Workspace,
        kind: str,
        label: str,
        *,
        enforce_chat_capacity: bool = True,
    ) -> Iterator[None]:
        if enforce_chat_capacity:
            runtime = self.settings.runtime()
        operation = ConversationOperation(
            user_id=workspace.user_id,
            kind=kind,
            label=label,
            started_at=utcnow(),
        )
        with self._operation_lock:
            active = self._operations.get(workspace.id)
            if active is not None:
                raise self._busy_error(active)
            if enforce_chat_capacity:
                chat_operations = tuple(
                    active
                    for active in self._operations.values()
                    if active.kind not in {"generation_submission", "workspace_mutation"}
                )
                user_operations = sum(
                    active.user_id == workspace.user_id for active in chat_operations
                )
                if user_operations >= runtime.max_concurrent_chats_per_user:
                    raise ServiceError(
                        f"同一账户最多同时进行 {runtime.max_concurrent_chats_per_user} 个 AI 对话请求",
                        code="conversation_user_limit",
                        status_code=429,
                    )
                if len(chat_operations) >= runtime.max_concurrent_chats:
                    raise ServiceError(
                        "当前 AI 对话请求较多，请稍后重试",
                        code="conversation_capacity",
                        status_code=503,
                    )
            self._operations[workspace.id] = operation
        try:
            yield
        finally:
            with self._operation_lock:
                if self._operations.get(workspace.id) is operation:
                    self._operations.pop(workspace.id, None)

    @staticmethod
    def _busy_error(operation: ConversationOperation) -> ServiceError:
        return ServiceError(
            f"{operation.label}，请完成后再继续",
            code="conversation_busy",
            status_code=409,
        )
