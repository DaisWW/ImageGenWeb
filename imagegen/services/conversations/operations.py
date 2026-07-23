from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, Lock
from time import monotonic
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
    operation_id: str = ""
    message_id: str = ""
    cancel_event: Event = field(default_factory=Event, compare=False, repr=False)

    def ensure_active(self) -> None:
        if self.cancel_event.is_set():
            raise ServiceError(
                "请求已取消",
                code="conversation_canceled",
                status_code=409,
            )

    def public_dict(self) -> dict[str, Any]:
        return {
            "busy": True,
            "kind": self.kind,
            "label": self.label,
            "started_at": self.started_at.isoformat(),
            "operation_id": self.operation_id,
            "message_id": self.message_id,
        }


class ConversationOperationRegistry:
    def __init__(self, settings: SystemSettingsService):
        self.settings = settings
        self._operation_lock = Lock()
        self._operations: dict[str, ConversationOperation] = {}
        self._inflight_chats: dict[int, ConversationOperation] = {}
        self._canceled_operations: dict[tuple[str, str], float] = {}

    def state(self, workspace_id: str) -> dict[str, Any]:
        with self._operation_lock:
            operation = self._operations.get(workspace_id)
        if operation is None:
            return {"busy": False, "kind": "", "label": "", "started_at": None}
        return operation.public_dict()

    def cancel(self, workspace_id: str, operation_id: str) -> bool:
        """Mark one operation canceled and release the workspace immediately.

        A short-lived tombstone covers the race where the cancel request arrives
        just before the original request registers its operation.
        """
        operation_id = str(operation_id or "").strip().lower()
        if not operation_id:
            return False
        with self._operation_lock:
            self._prune_canceled_locked()
            operation = self._operations.get(workspace_id)
            if operation is not None and operation_id in {
                operation.operation_id,
                operation.message_id,
            }:
                operation.cancel_event.set()
                self._operations.pop(workspace_id, None)
                return True
            self._canceled_operations[(workspace_id, operation_id)] = monotonic()
        return False

    @contextmanager
    def generation_submission(
        self,
        workspace: Workspace,
        *,
        operation_id: str = "",
    ) -> Iterator[ConversationOperation]:
        with self.workspace_operation(
            workspace,
            "generation_submission",
            "正在提交生成任务",
            enforce_chat_capacity=False,
            operation_id=operation_id,
        ) as operation:
            yield operation

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
        operation_id: str = "",
        message_id: str = "",
    ) -> Iterator[ConversationOperation]:
        if enforce_chat_capacity:
            runtime = self.settings.runtime()
        operation = ConversationOperation(
            user_id=workspace.user_id,
            kind=kind,
            label=label,
            started_at=utcnow(),
            operation_id=str(operation_id or "").strip().lower(),
            message_id=str(message_id or "").strip().lower(),
        )
        with self._operation_lock:
            self._prune_canceled_locked()
            canceled = [
                self._canceled_operations.pop((workspace.id, identifier), None)
                for identifier in (operation.operation_id, operation.message_id)
                if identifier
            ]
            if any(created is not None for created in canceled):
                operation.cancel_event.set()
                operation.ensure_active()
            active = self._operations.get(workspace.id)
            if active is not None:
                raise self._busy_error(active)
            if enforce_chat_capacity:
                chat_operations = tuple(self._inflight_chats.values())
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
                self._inflight_chats[id(operation)] = operation
            self._operations[workspace.id] = operation
        try:
            yield operation
        finally:
            with self._operation_lock:
                if self._operations.get(workspace.id) is operation:
                    self._operations.pop(workspace.id, None)
                self._inflight_chats.pop(id(operation), None)

    @staticmethod
    def _busy_error(operation: ConversationOperation) -> ServiceError:
        return ServiceError(
            f"{operation.label}，请完成后再继续",
            code="conversation_busy",
            status_code=409,
        )

    def _prune_canceled_locked(self) -> None:
        cutoff = monotonic() - 60
        self._canceled_operations = {
            key: created for key, created in self._canceled_operations.items() if created >= cutoff
        }
