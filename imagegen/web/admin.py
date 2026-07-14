from __future__ import annotations

from flask import jsonify
from flask_login import current_user
from sqlalchemy import func, select

from ..errors import ServiceError
from ..extensions import db
from ..models import GenerationItem, User
from ..serializers import job_dict, user_dict
from ..services import SpendingSummary
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
    try:
        concurrency = int(data.get("generation_concurrency", 2))
    except (TypeError, ValueError) as exc:
        raise ServiceError("用户并发必须是整数") from exc
    user = services().users.create(
        username=str(data.get("username", "")),
        display_name=str(data.get("display_name", "")),
        password=str(data.get("password", "")),
        balance_rmb=data.get("balance_rmb", "0"),
        generation_concurrency=concurrency,
        actor_user_id=current_user.id,
    )
    return jsonify(user=user_dict(user)), 201


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
    jobs = generation_service.list_jobs(admin=True, limit=query_limit())
    positions = generation_service.queue_positions()
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
        running_images=db.session.scalar(
            select(func.count(GenerationItem.id)).where(
                GenerationItem.status.in_(["running", "canceling"])
            )
        )
        or 0,
        queued_images=db.session.scalar(
            select(func.count(GenerationItem.id)).where(GenerationItem.status == "queued")
        )
        or 0,
    )


@web.post("/api/admin/generations/<job_id>/cancel")
@admin_required
def admin_cancel_generation(job_id: str):
    job = services().generations.cancel(job_id, admin=True)
    return jsonify(job=job_dict(job, channels(), admin=True))


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
    return jsonify(
        site_title=services().settings.site_title(),
        version=__version__,
    )


@web.put("/api/admin/settings")
@admin_required
def admin_update_settings():
    title = services().settings.set_site_title(
        str(json_body().get("site_title", "")), current_user.id
    )
    return jsonify(site_title=title, version=__version__)
