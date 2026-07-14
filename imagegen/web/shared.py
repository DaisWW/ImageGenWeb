from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import abort, current_app, request
from flask_login import current_user, login_required
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..config import ChannelRegistry, ChatModelRegistry
from ..container import ApplicationServices
from ..errors import ServiceError
from ..extensions import db
from ..models import GenerationItem, GenerationJob, Workspace
from ..serializers import workspace_dict
from ..storage import ImageStorage
from ..validation import as_bool


def services() -> ApplicationServices:
    return current_app.extensions["imagegen_services"]


def channels() -> ChannelRegistry:
    return current_app.extensions["channel_registry"]


def chat_models() -> ChatModelRegistry:
    return current_app.extensions["chat_model_registry"]


def storage() -> ImageStorage:
    return current_app.extensions["image_storage"]


def workspace_payload(workspace: Workspace) -> dict[str, Any]:
    payload = workspace_dict(workspace)
    payload["conversation_operation"] = services().conversations.operation_state(workspace.id)
    return payload


def admin_required(view: Callable):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def owned_workspace(workspace_id: str) -> Workspace:
    workspace = db.session.scalar(
        select(Workspace)
        .options(selectinload(Workspace.assets))
        .where(
            Workspace.id == workspace_id,
            Workspace.user_id == current_user.id,
        )
    )
    if workspace is None:
        raise ServiceError("工作站不存在", status_code=404)
    return workspace


def accessible_item(item_id: str) -> GenerationItem:
    query = (
        select(GenerationItem)
        .options(selectinload(GenerationItem.job))
        .join(GenerationJob)
        .where(GenerationItem.id == item_id)
    )
    if not current_user.is_admin:
        query = query.where(GenerationItem.user_id == current_user.id)
    item = db.session.scalar(query)
    if item is None:
        abort(404)
    return item


def json_body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ServiceError("请求内容必须是 JSON")
    return data


def query_limit() -> int:
    try:
        return min(200, max(1, int(request.args.get("limit", "100"))))
    except ValueError:
        return 100


def image_extension(mime_type: str | None) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }.get(mime_type or "", "img")


json_bool = as_bool
