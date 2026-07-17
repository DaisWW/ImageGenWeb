from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..errors import ServiceError
from ..extensions import db
from ..models import AuditLog, SystemState, utcnow
from ..validation import as_bool

SYSTEM_SETTINGS_KEY = "runtime_config.system.v1"
MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    default_user_concurrency: int = 2
    max_workspaces_per_user: int = 10
    max_assets_per_workspace: int = 20
    create_starter_workspaces: bool = True
    max_message_characters: int = 12000
    max_chat_attachments: int = 20
    max_attachment_mb: int = 10
    max_attachment_total_mb: int = 40
    max_concurrent_chats: int = 4
    max_concurrent_chats_per_user: int = 2
    max_prompt_characters: int = 8000
    max_batch_images: int = 20
    max_animation_frames: int = 20
    max_animation_fps: int = 24
    worker_poll_milliseconds: int = 500
    worker_heartbeat_seconds: int = 15
    worker_recovery_seconds: int = 60
    cleanup_interval_minutes: int = 60
    runtime_log_retention_days: int = 30

    @property
    def max_attachment_bytes(self) -> int:
        return self.max_attachment_mb * MIB

    @property
    def max_attachment_total_bytes(self) -> int:
        return self.max_attachment_total_mb * MIB

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)

    def client_dict(self) -> dict[str, int | bool]:
        keys = {
            "max_workspaces_per_user",
            "max_assets_per_workspace",
            "max_message_characters",
            "max_chat_attachments",
            "max_attachment_mb",
            "max_attachment_total_mb",
            "max_prompt_characters",
            "max_batch_images",
            "max_animation_frames",
            "max_animation_fps",
        }
        return {key: value for key, value in self.as_dict().items() if key in keys}


@dataclass(frozen=True, slots=True)
class _SettingsSnapshot:
    runtime_value: str | None
    title_value: str | None
    runtime: RuntimeSettings
    site_title: str


class SystemSettingsService:
    SITE_TITLE_KEY = "site_title"
    DEFAULT_SITE_TITLE = "西郊比克王 AI Studio"
    REFRESH_INTERVAL_SECONDS = 1.0

    def __init__(self):
        self._snapshot_lock = threading.RLock()
        self._snapshot: _SettingsSnapshot | None = None
        self._next_refresh = 0.0

    def site_title(self) -> str:
        return self._settings_snapshot().site_title

    def runtime(self) -> RuntimeSettings:
        return self._settings_snapshot().runtime

    def editable_config(self) -> dict[str, Any]:
        return self._config_dict(self._settings_snapshot(force=True))

    def _config_dict(self, snapshot: _SettingsSnapshot) -> dict[str, Any]:
        return {
            "site_title": snapshot.site_title,
            "runtime": snapshot.runtime.as_dict(),
            "revision": self._revision(snapshot.runtime_value, snapshot.title_value),
            "managed": bool(snapshot.runtime_value),
        }

    def save(self, payload: Any, actor_user_id: int) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ServiceError("系统配置必须是对象")
        title = self._validate_title(str(payload.get("site_title", "")))
        raw_runtime = payload.get("runtime")
        if not isinstance(raw_runtime, dict):
            raise ServiceError("运行参数必须是对象")
        try:
            runtime = _parse_runtime_settings(raw_runtime)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc

        state = db.session.get(SystemState, SYSTEM_SETTINGS_KEY)
        title_state = db.session.get(SystemState, self.SITE_TITLE_KEY)
        current_value = state.value if state is not None else None
        current_title_value = title_state.value if title_state is not None else None
        current_revision = self._revision(current_value, current_title_value)
        if str(payload.get("revision", "")) != current_revision:
            self._raise_conflict()
        old_runtime = self._runtime_from_value(current_value)
        old_title = self._title_from_value(current_title_value)

        serialized = json.dumps(
            {"schema": 1, "settings": runtime.as_dict()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._compare_and_set(
            key=SYSTEM_SETTINGS_KEY,
            state=state,
            current_value=current_value,
            new_value=serialized,
        )
        self._compare_and_set(
            key=self.SITE_TITLE_KEY,
            state=title_state,
            current_value=current_title_value,
            new_value=title,
        )
        revision = self._revision(serialized, title)
        changed = [
            key for key, value in runtime.as_dict().items() if value != getattr(old_runtime, key)
        ]
        if old_title != title:
            changed.insert(0, "site_title")
        db.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action="system.settings.update",
                target_type="system",
                target_id=SYSTEM_SETTINGS_KEY,
                details={"revision": revision, "changed": changed},
            )
        )
        db.session.commit()
        snapshot = self._publish_snapshot(serialized, title, runtime)
        return self._config_dict(snapshot)

    def _settings_snapshot(self, *, force: bool = False) -> _SettingsSnapshot:
        now = time.monotonic()
        with self._snapshot_lock:
            if not force and self._snapshot is not None and now < self._next_refresh:
                return self._snapshot
            values = dict(
                db.session.execute(
                    select(SystemState.key, SystemState.value).where(
                        SystemState.key.in_((SYSTEM_SETTINGS_KEY, self.SITE_TITLE_KEY))
                    )
                ).all()
            )
            return self._publish_snapshot(
                values.get(SYSTEM_SETTINGS_KEY),
                values.get(self.SITE_TITLE_KEY),
            )

    def _publish_snapshot(
        self,
        runtime_value: str | None,
        title_value: str | None,
        runtime: RuntimeSettings | None = None,
    ) -> _SettingsSnapshot:
        snapshot = _SettingsSnapshot(
            runtime_value=runtime_value,
            title_value=title_value,
            runtime=runtime or self._parse_runtime_value(runtime_value),
            site_title=self._title_from_value(title_value),
        )
        with self._snapshot_lock:
            self._snapshot = snapshot
            self._next_refresh = time.monotonic() + self.REFRESH_INTERVAL_SECONDS
        return snapshot

    @staticmethod
    def _validate_title(title: str) -> str:
        title = title.strip()
        if not 2 <= len(title) <= 60 or "\n" in title:
            raise ServiceError("系统 Title 长度必须在 2 到 60 个字符之间")
        return title

    @staticmethod
    def _title_from_value(value: str | None) -> str:
        return value if value and value.strip() else SystemSettingsService.DEFAULT_SITE_TITLE

    @staticmethod
    def _revision(runtime_value: str | None, title_value: str | None) -> str:
        if runtime_value is None and title_value is None:
            return ""
        value = json.dumps(
            {"runtime": runtime_value, "site_title": title_value},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _compare_and_set(
        self,
        *,
        key: str,
        state: SystemState | None,
        current_value: str | None,
        new_value: str,
    ) -> None:
        if state is None:
            db.session.add(SystemState(key=key, value=new_value))
            try:
                db.session.flush()
            except IntegrityError as exc:
                db.session.rollback()
                raise self._conflict_error() from exc
            return
        updated = db.session.execute(
            update(SystemState)
            .where(SystemState.key == key, SystemState.value == current_value)
            .values(value=new_value, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount != 1:
            self._raise_conflict()
        db.session.expire(state)

    @staticmethod
    def _runtime_from_value(serialized: str | None) -> RuntimeSettings:
        try:
            return SystemSettingsService._parse_runtime_value(serialized)
        except RuntimeError:
            return RuntimeSettings()

    @staticmethod
    def _parse_runtime_value(serialized: str | None) -> RuntimeSettings:
        if not serialized:
            return RuntimeSettings()
        try:
            payload = json.loads(serialized)
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != 1
                or not isinstance(payload.get("settings"), dict)
            ):
                raise ValueError("系统配置文档格式无效")
            return _parse_runtime_settings(payload["settings"])
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"数据库系统配置损坏：{exc}") from exc

    @staticmethod
    def _conflict_error() -> ServiceError:
        return ServiceError(
            "系统配置已被其他管理员更新，请刷新后重试",
            code="config_conflict",
            status_code=409,
        )

    def _raise_conflict(self) -> None:
        db.session.rollback()
        with self._snapshot_lock:
            self._next_refresh = 0.0
        raise self._conflict_error()


def _parse_runtime_settings(raw: dict[str, Any]) -> RuntimeSettings:
    settings = RuntimeSettings(
        default_user_concurrency=_bounded_int(raw, "default_user_concurrency", 2, 1, 16),
        max_workspaces_per_user=_bounded_int(raw, "max_workspaces_per_user", 10, 2, 100),
        max_assets_per_workspace=_bounded_int(raw, "max_assets_per_workspace", 20, 1, 32),
        create_starter_workspaces=as_bool(raw.get("create_starter_workspaces", True)),
        max_message_characters=_bounded_int(raw, "max_message_characters", 12000, 100, 50000),
        max_chat_attachments=_bounded_int(raw, "max_chat_attachments", 20, 1, 32),
        max_attachment_mb=_bounded_int(raw, "max_attachment_mb", 10, 1, 40),
        max_attachment_total_mb=_bounded_int(raw, "max_attachment_total_mb", 40, 1, 40),
        max_concurrent_chats=_bounded_int(raw, "max_concurrent_chats", 4, 1, 64),
        max_concurrent_chats_per_user=_bounded_int(raw, "max_concurrent_chats_per_user", 2, 1, 16),
        max_prompt_characters=_bounded_int(raw, "max_prompt_characters", 8000, 1000, 12000),
        max_batch_images=_bounded_int(raw, "max_batch_images", 20, 1, 100),
        max_animation_frames=_bounded_int(raw, "max_animation_frames", 20, 2, 100),
        max_animation_fps=_bounded_int(raw, "max_animation_fps", 24, 1, 60),
        worker_poll_milliseconds=_bounded_int(raw, "worker_poll_milliseconds", 500, 100, 10000),
        worker_heartbeat_seconds=_bounded_int(raw, "worker_heartbeat_seconds", 15, 5, 120),
        worker_recovery_seconds=_bounded_int(raw, "worker_recovery_seconds", 60, 10, 3600),
        cleanup_interval_minutes=_bounded_int(raw, "cleanup_interval_minutes", 60, 5, 1440),
        runtime_log_retention_days=_bounded_int(raw, "runtime_log_retention_days", 30, 1, 365),
    )
    if settings.max_chat_attachments > settings.max_assets_per_workspace:
        raise ValueError("单条消息附件数不能超过每个工作站素材数")
    if settings.max_attachment_mb > settings.max_attachment_total_mb:
        raise ValueError("单张附件上限不能超过附件合计上限")
    if settings.max_concurrent_chats_per_user > settings.max_concurrent_chats:
        raise ValueError("单用户对话并发不能超过全局对话并发")
    return settings


def _bounded_int(raw: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"系统参数 {key} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"系统参数 {key} 必须在 {minimum} 到 {maximum} 之间")
    return value
