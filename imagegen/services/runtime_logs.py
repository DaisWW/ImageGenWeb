from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select

from ..extensions import db
from ..models import AuditLog, RuntimeLog, User, utcnow

LOGGER = logging.getLogger(__name__)

_SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "b64json",
    "body",
    "content",
    "cookie",
    "image",
    "messages",
    "password",
    "prompt",
    "secret",
    "token",
}
_REDACT_PATTERNS = (
    re.compile(r"(?i)bearer\s+[^\s,;]+"),
    re.compile(
        r"(?i)(?:api[_ -]?key|authorization|token|secret|password)\s*[:=]\s*['\"]?[^\s,'\"]+"
    ),
    re.compile(r"(?i)data:[^;\s]+;base64,[a-z0-9+/=]+"),
)
_MAX_DETAIL_BYTES = 12000
_MAX_TEXT_LENGTH = 1000


class RuntimeLogService:
    """保存经过脱敏的运行事件，并提供管理员查询。"""

    def record(
        self,
        *,
        category: str,
        event: str,
        status: str,
        message: str = "",
        source: str = "web",
        level: str | None = None,
        user_id: int | None = None,
        user_label: str = "",
        workspace_id: str = "",
        workspace_label: str = "",
        job_id: str = "",
        item_id: str = "",
        provider_id: str = "",
        provider_label: str = "",
        model: str = "",
        error_code: str = "",
        http_status: int | None = None,
        upstream_request_id: str = "",
        elapsed_seconds: float | None = None,
        details: Any = None,
    ) -> RuntimeLog:
        safe_status = _clip(status, 20)
        entry = RuntimeLog(
            level=_clip(level or ("error" if safe_status == "error" else "info"), 12),
            category=_clip(category, 30),
            event=_clip(event, 80),
            status=safe_status,
            source=_clip(source, 30),
            message=_clip(_redact_text(message), _MAX_TEXT_LENGTH),
            user_id=user_id,
            user_label=_clip(_redact_text(user_label), 120),
            workspace_id=_clip(workspace_id, 32),
            workspace_label=_clip(_redact_text(workspace_label), 100),
            job_id=_clip(job_id, 32),
            item_id=_clip(item_id, 32),
            provider_id=_clip(provider_id, 64),
            provider_label=_clip(_redact_text(provider_label), 100),
            model=_clip(_redact_text(model), 150),
            error_code=_clip(error_code, 80),
            http_status=http_status,
            upstream_request_id=_clip(upstream_request_id, 255),
            elapsed_seconds=elapsed_seconds,
            details=sanitize_details(details),
        )
        db.session.add(entry)
        return entry

    def commit_best_effort(self, **kwargs: Any) -> RuntimeLog | None:
        """尽力写入事件，不能让日志失败掩盖原始操作。"""
        try:
            entry = self.record(**kwargs)
            db.session.commit()
            return entry
        except Exception:
            db.session.rollback()
            LOGGER.exception("无法保存运行日志")
            return None

    def purge(self, retention_days: int) -> int:
        cutoff = utcnow() - timedelta(days=max(1, retention_days))
        result = db.session.execute(delete(RuntimeLog).where(RuntimeLog.created_at < cutoff))
        db.session.commit()
        return int(result.rowcount or 0)

    def list_runtime(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        category: str = "",
        status: str = "",
        user_id: int | None = None,
        model: str = "",
        error_code: str = "",
        search: str = "",
        since_hours: int | None = 168,
    ) -> tuple[list[RuntimeLog], int]:
        filters = self._runtime_filters(
            category=category,
            status=status,
            user_id=user_id,
            model=model,
            error_code=error_code,
            search=search,
            since_hours=since_hours,
        )
        total = db.session.scalar(select(func.count(RuntimeLog.id)).where(*filters)) or 0
        entries = list(
            db.session.scalars(
                select(RuntimeLog)
                .where(*filters)
                .order_by(RuntimeLog.created_at.desc(), RuntimeLog.id.desc())
                .offset(max(0, offset))
                .limit(min(200, max(1, limit)))
            )
        )
        return entries, int(total)

    def get_runtime(self, log_id: str) -> RuntimeLog | None:
        return db.session.get(RuntimeLog, log_id)

    def list_audit(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        actor_user_id: int | None = None,
        action: str = "",
        search: str = "",
        since_hours: int | None = 720,
    ) -> tuple[list[tuple[AuditLog, User | None]], int]:
        filters = []
        if actor_user_id is not None:
            filters.append(AuditLog.actor_user_id == actor_user_id)
        if action:
            filters.append(AuditLog.action == action[:80])
        if since_hours is not None:
            filters.append(AuditLog.created_at >= utcnow() - timedelta(hours=max(1, since_hours)))
        if search:
            pattern = f"%{search[:100]}%"
            filters.append(
                or_(
                    AuditLog.action.ilike(pattern),
                    AuditLog.target_type.ilike(pattern),
                    AuditLog.target_id.ilike(pattern),
                )
            )
        total = db.session.scalar(select(func.count(AuditLog.id)).where(*filters)) or 0
        rows = list(
            db.session.execute(
                select(AuditLog, User)
                .outerjoin(User, User.id == AuditLog.actor_user_id)
                .where(*filters)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .offset(max(0, offset))
                .limit(min(200, max(1, limit)))
            )
        )
        return rows, int(total)

    def get_audit(self, log_id: int) -> tuple[AuditLog, User | None] | None:
        row = db.session.execute(
            select(AuditLog, User)
            .outerjoin(User, User.id == AuditLog.actor_user_id)
            .where(AuditLog.id == log_id)
        ).first()
        return row

    @staticmethod
    def _runtime_filters(
        *,
        category: str,
        status: str,
        user_id: int | None,
        model: str,
        error_code: str,
        search: str,
        since_hours: int | None,
    ) -> list[Any]:
        filters: list[Any] = []
        if category:
            filters.append(RuntimeLog.category == category[:30])
        if status:
            filters.append(RuntimeLog.status == status[:20])
        if user_id is not None:
            filters.append(RuntimeLog.user_id == user_id)
        if model:
            pattern = f"%{model[:150]}%"
            filters.append(
                or_(
                    RuntimeLog.model.ilike(pattern),
                    RuntimeLog.provider_id.ilike(pattern),
                    RuntimeLog.provider_label.ilike(pattern),
                )
            )
        if error_code:
            filters.append(RuntimeLog.error_code == error_code[:80])
        if since_hours is not None:
            filters.append(RuntimeLog.created_at >= utcnow() - timedelta(hours=max(1, since_hours)))
        if search:
            pattern = f"%{search[:100]}%"
            filters.append(
                or_(
                    RuntimeLog.id.ilike(pattern),
                    RuntimeLog.event.ilike(pattern),
                    RuntimeLog.message.ilike(pattern),
                    RuntimeLog.provider_label.ilike(pattern),
                    RuntimeLog.model.ilike(pattern),
                    RuntimeLog.error_code.ilike(pattern),
                    RuntimeLog.upstream_request_id.ilike(pattern),
                    RuntimeLog.user_label.ilike(pattern),
                    RuntimeLog.workspace_label.ilike(pattern),
                    RuntimeLog.job_id.ilike(pattern),
                    RuntimeLog.item_id.ilike(pattern),
                )
            )
        return filters


def sanitize_details(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    sanitized = _sanitize_value(value, key="", depth=0)
    if not isinstance(sanitized, dict):
        sanitized = {"value": sanitized}
    try:
        if (
            len(json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")))
            > _MAX_DETAIL_BYTES
        ):
            return {"_truncated": True, "keys": list(sanitized)[:50]}
    except (TypeError, ValueError):
        return {"_unavailable": True}
    return sanitized


def _sanitize_value(value: Any, *, key: str, depth: int) -> Any:
    if depth > 4:
        return "[已截断]"
    normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith("apikey"):
        return "[已隐藏]"
    if isinstance(value, dict):
        return {
            str(item_key)[:80]: _sanitize_value(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in list(value.items())[:50]
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, key="", depth=depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return _clip(_redact_text(value), _MAX_TEXT_LENGTH)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clip(_redact_text(str(value)), _MAX_TEXT_LENGTH)


def _redact_text(value: str) -> str:
    text = str(value or "")
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[已隐藏]", text)
    return text


def _clip(value: str, length: int) -> str:
    return str(value or "")[:length]


def runtime_log_dict(entry: RuntimeLog, *, include_details: bool = False) -> dict[str, Any]:
    payload = {
        "id": entry.id,
        "level": entry.level,
        "category": entry.category,
        "event": entry.event,
        "status": entry.status,
        "source": entry.source,
        "message": entry.message,
        "user_id": entry.user_id,
        "user_label": entry.user_label,
        "workspace_id": entry.workspace_id,
        "workspace_label": entry.workspace_label,
        "job_id": entry.job_id,
        "item_id": entry.item_id,
        "provider_id": entry.provider_id,
        "provider_label": entry.provider_label,
        "model": entry.model,
        "error_code": entry.error_code,
        "http_status": entry.http_status,
        "upstream_request_id": entry.upstream_request_id,
        "elapsed_seconds": (
            None if entry.elapsed_seconds is None else float(entry.elapsed_seconds)
        ),
        "created_at": entry.created_at.isoformat(),
    }
    if include_details:
        payload["details"] = sanitize_details(entry.details)
    return payload


def audit_log_dict(
    entry: AuditLog, actor: User | None, *, include_details: bool = False
) -> dict[str, Any]:
    payload = {
        "id": entry.id,
        "actor_user_id": entry.actor_user_id,
        "actor_label": (
            (actor.display_name or actor.username) if actor is not None else "已删除账户"
        ),
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "created_at": entry.created_at.isoformat(),
    }
    if include_details:
        payload["details"] = sanitize_details(entry.details)
    return payload
