from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from threading import Condition, Event, Lock
from time import monotonic
from typing import Any, ClassVar
from uuid import uuid4

from ...errors import ServiceError
from ...integrations.openai_chat import ChatProgress
from ...models import Workspace, utcnow
from ..settings import SystemSettingsService


@dataclass
class ConversationOperation:
    user_id: int
    kind: str
    label: str
    started_at: datetime
    operation_id: str = ""
    message_id: str = ""
    stage: str = "preparing"
    stage_label: str = "正在准备请求"
    first_output_seconds: float | None = None
    output_characters: int = 0
    request_body_bytes: int | None = None
    last_event_at: datetime | None = None
    preview_text: str = field(default="", compare=False, repr=False)
    preview_version: int = field(default=0, compare=False, repr=False)
    finished: bool = field(default=False, compare=False, repr=False)
    cancel_event: Event = field(default_factory=Event, compare=False, repr=False)
    progress_lock: Lock = field(default_factory=Lock, compare=False, repr=False)
    preview_condition: Condition = field(init=False, compare=False, repr=False)
    started_monotonic: float = field(default_factory=monotonic, compare=False, repr=False)

    _CLIENT_STAGE_LABELS: ClassVar[dict[str, str]] = {
        "request_prepared": "请求已准备，正在连接上游模型",
        "connecting": "正在连接上游模型",
        "upstream_connected": "已连接上游，等待模型首个输出",
        "reasoning": "模型正在分析需求",
        "output_started": "已收到模型首个输出",
        "output": "模型正在输出",
        "completed": "模型输出完成，正在解析结果",
        "upstream_error": "上游模型返回错误",
        "connection_error": "上游连接中断",
        "timeout": "等待模型响应超时",
    }
    _STAGE_RANKS: ClassVar[dict[str, int]] = {
        "preparing": 0,
        "context": 10,
        "request_prepared": 20,
        "connecting": 30,
        "upstream_connected": 40,
        "reasoning": 50,
        "output_started": 60,
        "output": 65,
        "completed": 70,
        "parsing": 80,
        "repairing": 85,
        "saving": 90,
        "upstream_error": 100,
        "connection_error": 100,
        "timeout": 100,
    }
    _ERROR_STAGES: ClassVar[frozenset[str]] = frozenset(
        {"upstream_error", "connection_error", "timeout"}
    )

    def __post_init__(self) -> None:
        self.preview_condition = Condition(self.progress_lock)

    def ensure_active(self) -> None:
        if self.cancel_event.is_set():
            raise ServiceError(
                "请求已取消",
                code="conversation_canceled",
                status_code=409,
            )

    def update_progress(
        self,
        stage: str,
        label: str,
        *,
        first_output_seconds: float | None = None,
        output_characters: int | None = None,
        request_body_bytes: int | None = None,
    ) -> None:
        with self.progress_lock:
            next_stage = str(stage or "processing")
            current_rank = self._STAGE_RANKS.get(self.stage)
            next_rank = self._STAGE_RANKS.get(next_stage)
            can_advance = not (
                self.stage in self._ERROR_STAGES and next_stage not in self._ERROR_STAGES
            ) and not (
                current_rank is not None and next_rank is not None and next_rank < current_rank
            )
            if can_advance:
                self.stage = next_stage
                self.stage_label = str(label or self.label)
            if first_output_seconds is not None:
                value = round(max(0.0, float(first_output_seconds)), 3)
                if self.first_output_seconds is None or value < self.first_output_seconds:
                    self.first_output_seconds = value
            if output_characters is not None:
                self.output_characters = max(self.output_characters, int(output_characters))
            if request_body_bytes is not None:
                self.request_body_bytes = max(self.request_body_bytes or 0, int(request_body_bytes))
            self.last_event_at = utcnow()

    def client_progress(self, progress: ChatProgress) -> None:
        self.update_progress(
            progress.stage,
            self._CLIENT_STAGE_LABELS.get(progress.stage, "正在处理请求"),
            first_output_seconds=progress.first_output_seconds,
            output_characters=progress.output_characters,
            request_body_bytes=progress.request_body_bytes,
        )

    def start_retry(self, model_label: str) -> None:
        with self.progress_lock:
            self.stage = "connecting"
            self.stage_label = f"正在切换到备用模型 {model_label}"
            self.last_event_at = utcnow()

    def update_preview(self, text: str) -> None:
        preview = str(text or "")
        if not preview:
            return
        with self.preview_condition:
            if self.finished or preview == self.preview_text:
                return
            self.preview_text = preview
            self.preview_version += 1
            self.preview_condition.notify_all()

    def finish(self) -> None:
        with self.preview_condition:
            self.finished = True
            self.preview_condition.notify_all()

    def wait_for_preview(
        self,
        version: int,
        timeout: float,
    ) -> tuple[int, str, bool, bool]:
        with self.preview_condition:
            changed = self.preview_condition.wait_for(
                lambda: self.preview_version > version or self.finished,
                timeout=timeout,
            )
            return self.preview_version, self.preview_text, self.finished, changed

    def public_dict(self) -> dict[str, Any]:
        with self.progress_lock:
            return {
                "busy": True,
                "kind": self.kind,
                "label": self.label,
                "stage": self.stage,
                "stage_label": self.stage_label,
                "elapsed_seconds": round(monotonic() - self.started_monotonic, 3),
                "first_output_seconds": self.first_output_seconds,
                "output_characters": self.output_characters,
                "request_body_bytes": self.request_body_bytes,
                "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
                "cancel_requested": self.cancel_event.is_set(),
                "started_at": self.started_at.isoformat(),
                "operation_id": self.operation_id,
                "message_id": self.message_id,
            }


class ConversationOperationRegistry:
    def __init__(self, settings: SystemSettingsService):
        self.settings = settings
        self._operation_lock = Lock()
        self._operation_condition = Condition(self._operation_lock)
        self._operations: dict[str, dict[str, ConversationOperation]] = {}
        self._mutation_workspaces: set[str] = set()
        self._canceled_operations: dict[tuple[str, str], float] = {}
        self._preview_reservations: dict[tuple[str, str], tuple[int, float]] = {}
        self._preview_streams: dict[tuple[str, str], int] = {}

    def state(self, workspace_id: str) -> dict[str, Any]:
        with self._operation_lock:
            operations = list(self._operations.get(workspace_id, {}).values())
        public_operations = [operation.public_dict() for operation in operations]
        state = {
            "busy": bool(public_operations),
            "operations": public_operations,
            "kind": "",
            "label": "",
            "stage": "",
            "stage_label": "",
            "elapsed_seconds": 0,
            "first_output_seconds": None,
            "output_characters": 0,
            "request_body_bytes": None,
            "last_event_at": None,
            "cancel_requested": False,
            "started_at": None,
            "operation_id": "",
            "message_id": "",
        }
        if public_operations:
            state.update(public_operations[0])
            state["operations"] = public_operations
        return state

    def cancel(self, workspace_id: str, operation_id: str) -> bool:
        """Mark one operation canceled; release the workspace when it returns.

        A short-lived tombstone covers the race where the cancel request arrives
        just before the original request registers its operation.
        """
        operation_id = str(operation_id or "").strip().lower()
        if not operation_id:
            return False
        with self._operation_lock:
            self._prune_canceled_locked()
            operation = self._find_operation_locked(workspace_id, operation_id)
            if operation is not None:
                operation.cancel_event.set()
                return True
            self._canceled_operations[(workspace_id, operation_id)] = monotonic()
        return False

    def reserve_preview(self, workspace_id: str, operation_id: str, user_id: int) -> None:
        operation_id = self._validated_operation_id(operation_id)
        runtime = self.settings.runtime()
        key = (workspace_id, operation_id)
        with self._operation_condition:
            self._prune_preview_reservations_locked()
            if key in self._preview_streams:
                raise ServiceError(
                    "该操作已有预览连接",
                    code="preview_stream_exists",
                    status_code=409,
                )
            self._preview_reservations[key] = (
                user_id,
                monotonic() + runtime.preview_reservation_seconds,
            )

    def preview_events(
        self,
        workspace_id: str,
        operation_id: str,
        user_id: int,
    ) -> Iterator[dict[str, Any]]:
        operation_id = self._validated_operation_id(operation_id)
        runtime = self.settings.runtime()
        key = (workspace_id, operation_id)
        with self._operation_condition:
            self._prune_preview_reservations_locked()
            operation = self._find_operation_locked(workspace_id, operation_id)
            reservation = self._preview_reservations.pop(key, None)
            if operation is None and (reservation is None or reservation[0] != user_id):
                raise ServiceError("预览操作不存在", status_code=404)
            if key in self._preview_streams:
                raise ServiceError(
                    "该操作已有预览连接",
                    code="preview_stream_exists",
                    status_code=409,
                )
            self._preview_streams[key] = user_id
        return self._preview_event_iterator(
            workspace_id,
            operation_id,
            key,
            operation,
            timeout=float(runtime.preview_reservation_seconds),
        )

    def _preview_event_iterator(
        self,
        workspace_id: str,
        operation_id: str,
        key: tuple[str, str],
        operation: ConversationOperation | None,
        *,
        timeout: float,
    ) -> Iterator[dict[str, Any]]:
        try:
            if operation is None:
                operation = self._wait_for_operation(
                    workspace_id,
                    operation_id,
                    timeout=timeout,
                )
            if operation is None:
                yield {"event": "close", "data": {}}
                return
            yield from self._operation_preview_events(operation)
        finally:
            with self._operation_condition:
                self._preview_streams.pop(key, None)
                self._operation_condition.notify_all()

    @staticmethod
    def _operation_preview_events(
        operation: ConversationOperation,
    ) -> Iterator[dict[str, Any]]:
        version = -1
        while True:
            next_version, preview, finished, changed = operation.wait_for_preview(
                version,
                timeout=15.0,
            )
            if next_version != version:
                version = next_version
                if preview:
                    yield {"event": "preview", "data": {"text": preview}}
            elif not changed:
                yield {"event": "keepalive", "data": {}}
            if finished:
                yield {"event": "close", "data": {}}
                return

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
            operation_id=operation_id,
        ) as operation:
            yield operation

    @contextmanager
    def workspace_mutation(self, workspace: Workspace, label: str) -> Iterator[None]:
        with self.workspace_operation(
            workspace,
            "workspace_mutation",
            label,
        ):
            yield

    @contextmanager
    def workspace_operation(
        self,
        workspace: Workspace,
        kind: str,
        label: str,
        *,
        operation_id: str = "",
        message_id: str = "",
    ) -> Iterator[ConversationOperation]:
        operation = ConversationOperation(
            user_id=workspace.user_id,
            kind=kind,
            label=label,
            started_at=utcnow(),
            stage_label=label,
            operation_id=str(operation_id or "").strip().lower() or uuid4().hex,
            message_id=str(message_id or "").strip().lower(),
        )
        with self._operation_condition:
            self._prune_canceled_locked()
            canceled = [
                self._canceled_operations.pop((workspace.id, identifier), None)
                for identifier in (operation.operation_id, operation.message_id)
                if identifier
            ]
            if any(created is not None for created in canceled):
                operation.cancel_event.set()
                operation.ensure_active()
            identifiers = (operation.operation_id, operation.message_id)
            if any(
                self._find_operation_locked(workspace.id, identifier) is not None
                for identifier in identifiers
                if identifier
            ):
                raise ServiceError(
                    "操作 ID 已在使用",
                    code="conversation_operation_conflict",
                    status_code=409,
                )
            active = self._operations.get(workspace.id, {})
            if kind == "workspace_mutation":
                if active:
                    raise self._busy_error(next(iter(active.values())))
                self._mutation_workspaces.add(workspace.id)
            elif workspace.id in self._mutation_workspaces:
                raise ServiceError(
                    "工作站正在变更，请稍后再试",
                    code="conversation_busy",
                    status_code=409,
                )
            self._operations.setdefault(workspace.id, {})[operation.operation_id] = operation
            self._operation_condition.notify_all()
        try:
            yield operation
        finally:
            operation.finish()
            with self._operation_condition:
                operations = self._operations.get(workspace.id, {})
                if operations.get(operation.operation_id) is operation:
                    operations.pop(operation.operation_id, None)
                if not operations:
                    self._operations.pop(workspace.id, None)
                if kind == "workspace_mutation":
                    self._mutation_workspaces.discard(workspace.id)
                self._operation_condition.notify_all()

    def _wait_for_operation(
        self,
        workspace_id: str,
        operation_id: str,
        *,
        timeout: float,
    ) -> ConversationOperation | None:
        operation_id = str(operation_id or "").strip().lower()
        if not operation_id:
            return None
        deadline = monotonic() + timeout
        with self._operation_condition:
            while True:
                operation = self._find_operation_locked(workspace_id, operation_id)
                if operation is not None:
                    return operation
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                self._operation_condition.wait(remaining)

    def _find_operation_locked(
        self,
        workspace_id: str,
        operation_id: str,
    ) -> ConversationOperation | None:
        for operation in self._operations.get(workspace_id, {}).values():
            if operation_id in {operation.operation_id, operation.message_id}:
                return operation
        return None

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

    def _prune_preview_reservations_locked(self) -> None:
        now = monotonic()
        self._preview_reservations = {
            key: value for key, value in self._preview_reservations.items() if value[1] > now
        }

    @staticmethod
    def _validated_operation_id(value: str) -> str:
        operation_id = str(value or "").strip().lower()
        if len(operation_id) != 32 or any(
            character not in "0123456789abcdef" for character in operation_id
        ):
            raise ServiceError("操作 ID 无效", status_code=404)
        return operation_id
