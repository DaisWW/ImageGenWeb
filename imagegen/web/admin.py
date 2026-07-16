from __future__ import annotations

from flask import jsonify, request
from flask_login import current_user
from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import User, Workspace
from ..serializers import job_dict, user_dict
from ..services import SpendingSummary
from ..services.runtime_logs import audit_log_dict, runtime_log_dict
from ..version import __version__
from . import web
from .shared import admin_required, channels, json_body, query_limit, services


@web.get("/api/admin/users")
@admin_required
def admin_users():
    users = list(db.session.scalars(select(User).order_by(User.created_at.desc())))
    summaries = services().billing.spending_by_user(user.id for user in users)
    payload = []
    for user in users:
        item = user_dict(user)
        item["spending"] = summaries[user.id].public_dict()
        payload.append(item)
    return jsonify(
        users=payload,
        spending=SpendingSummary.combine(summaries.values()).public_dict(),
    )


@web.post("/api/admin/users")
@admin_required
def admin_create_user():
    data = json_body()
    application_services = services()
    try:
        concurrency = int(
            data.get(
                "generation_concurrency",
                application_services.settings.runtime().default_user_concurrency,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ServiceError("用户并发必须是整数") from exc
    try:
        user = application_services.users.create(
            username=str(data.get("username", "")),
            display_name=str(data.get("display_name", "")),
            password=str(data.get("password", "")),
            balance_rmb=data.get("balance_rmb", "0"),
            generation_concurrency=concurrency,
            actor_user_id=current_user.id,
            commit=False,
        )
        application_services.workspaces.ensure_starter_workspaces(user.id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return jsonify(user=user_dict(user)), 201


@web.put("/api/admin/users/<int:user_id>")
@admin_required
def admin_update_user(user_id: int):
    data = json_body()
    try:
        concurrency = int(data.get("generation_concurrency", 0))
    except (TypeError, ValueError) as exc:
        raise ServiceError("用户并发必须是整数") from exc
    user = services().users.update_profile(
        user_id,
        display_name=str(data.get("display_name", "")),
        generation_concurrency=concurrency,
        actor_user_id=current_user.id,
    )
    return jsonify(user=user_dict(user))


@web.post("/api/admin/users/<int:user_id>/balance")
@admin_required
def admin_adjust_balance(user_id: int):
    data = json_body()
    user = services().billing.adjust(
        user_id=user_id,
        actor_user_id=current_user.id,
        amount=data.get("amount_rmb", "0"),
        operation=str(data.get("operation", "add")),
        note=str(data.get("note", "")),
    )
    return jsonify(user=user_dict(user))


@web.post("/api/admin/users/<int:user_id>/status")
@admin_required
def admin_user_status(user_id: int):
    user = services().users.update_status(
        user_id, str(json_body().get("status", "")), current_user.id
    )
    return jsonify(user=user_dict(user))


@web.post("/api/admin/users/<int:user_id>/password")
@admin_required
def admin_reset_password(user_id: int):
    services().users.reset_password(
        user_id,
        str(json_body().get("password", "")),
        current_user.id,
    )
    return jsonify(ok=True)


@web.get("/api/admin/generations")
@admin_required
def admin_generations():
    generation_service = services().generations
    user_id = _positive_query_int("user_id", "用户筛选无效")
    workspace_id = request.args.get("workspace_id", "").strip() or None
    jobs = generation_service.list_jobs(
        admin=True,
        user_id=user_id,
        workspace_id=workspace_id,
        limit=query_limit(),
    )
    positions = generation_service.queue_positions()
    running_images, queued_images = generation_service.queue_item_counts(
        user_id=user_id,
        workspace_id=workspace_id,
    )
    return jsonify(
        jobs=[
            job_dict(
                job,
                channels(),
                queue_position=positions.get(job.id),
                queue_total=len(positions),
                admin=True,
            )
            for job in jobs
        ],
        queue_total=len(positions),
        running_images=running_images,
        queued_images=queued_images,
    )


@web.get("/api/admin/generation-filters")
@admin_required
def admin_generation_filters():
    users = list(db.session.scalars(select(User).order_by(User.created_at.desc())))
    workspaces = list(db.session.scalars(select(Workspace).order_by(Workspace.updated_at.desc())))
    return jsonify(
        users=[user_dict(user, include_private=False) for user in users],
        workspaces=[
            {
                "id": workspace.id,
                "name": workspace.name,
                "user_id": workspace.user_id,
            }
            for workspace in workspaces
        ],
    )


@web.post("/api/admin/generations/<job_id>/cancel")
@admin_required
def admin_cancel_generation(job_id: str):
    job = services().generations.cancel(job_id, admin=True)
    return jsonify(job=job_dict(job, channels(), admin=True))


@web.get("/api/admin/runtime-logs")
@admin_required
def admin_runtime_logs():
    offset = _log_offset()
    entries, total = services().runtime_logs.list_runtime(
        limit=query_limit(),
        offset=offset,
        category=request.args.get("category", "").strip(),
        status=request.args.get("status", "").strip(),
        user_id=_positive_query_int("user_id", "日志用户筛选无效"),
        model=request.args.get("model", "").strip(),
        error_code=request.args.get("error_code", "").strip(),
        search=request.args.get("search", "").strip(),
        since_hours=_log_since_hours(168),
    )
    return jsonify(
        logs=[runtime_log_dict(entry) for entry in entries],
        total=total,
        offset=offset,
    )


@web.get("/api/admin/runtime-logs/<log_id>")
@admin_required
def admin_runtime_log(log_id: str):
    entry = services().runtime_logs.get_runtime(log_id)
    if entry is None:
        raise ServiceError("运行日志不存在", status_code=404)
    return jsonify(log=runtime_log_dict(entry, include_details=True))


@web.get("/api/admin/audit-logs")
@admin_required
def admin_audit_logs():
    offset = _log_offset()
    rows, total = services().runtime_logs.list_audit(
        limit=query_limit(),
        offset=offset,
        actor_user_id=_positive_query_int("actor_user_id", "日志用户筛选无效"),
        action=request.args.get("action", "").strip(),
        search=request.args.get("search", "").strip(),
        since_hours=_log_since_hours(720),
    )
    return jsonify(
        logs=[audit_log_dict(entry, actor) for entry, actor in rows],
        total=total,
        offset=offset,
    )


@web.get("/api/admin/audit-logs/<int:log_id>")
@admin_required
def admin_audit_log(log_id: int):
    row = services().runtime_logs.get_audit(log_id)
    if row is None:
        raise ServiceError("审计日志不存在", status_code=404)
    entry, actor = row
    return jsonify(log=audit_log_dict(entry, actor, include_details=True))


def _positive_query_int(name: str, error_message: str) -> int | None:
    raw = request.args.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ServiceError(error_message) from exc
    if value < 1:
        raise ServiceError(error_message)
    return value


def _log_offset() -> int:
    try:
        return min(100000, max(0, int(request.args.get("offset", "0"))))
    except ValueError:
        return 0


def _log_since_hours(default: int) -> int | None:
    raw = request.args.get("since_hours", str(default)).strip().lower()
    if raw in {"", "all"}:
        return None
    try:
        return min(8760, max(1, int(raw)))
    except ValueError as exc:
        raise ServiceError("日志时间范围无效") from exc


@web.get("/api/admin/channels")
@admin_required
def admin_channels():
    return jsonify(config=services().configuration.channel_config())


@web.put("/api/admin/channels")
@admin_required
def admin_update_channels():
    config = services().configuration.save_channels(json_body(), current_user.id)
    return jsonify(config=config)


@web.get("/api/admin/chat-models")
@admin_required
def admin_chat_models():
    return jsonify(config=services().configuration.chat_config())


@web.put("/api/admin/chat-models")
@admin_required
def admin_update_chat_models():
    config = services().configuration.save_chat_models(json_body(), current_user.id)
    return jsonify(config=config)


@web.get("/api/admin/settings")
@admin_required
def admin_settings():
    return jsonify(**services().settings.editable_config(), version=__version__)


@web.put("/api/admin/settings")
@admin_required
def admin_update_settings():
    config = services().settings.save(json_body(), current_user.id)
    return jsonify(**config, version=__version__)
