from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import abort, current_app, request
from flask_login import current_user, login_required
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from werkzeug.datastructures import FileStorage

from ..config import ChannelRegistry, ChatModelRegistry, MattingModelRegistry
from ..container import ApplicationServices
from ..errors import ServiceError
from ..extensions import db
from ..image_masks import MAX_MASK_BYTES
from ..models import (
    IMAGE_TEST_KIND,
    INTERNAL_WORKSPACE_KINDS,
    GenerationItem,
    GenerationJob,
    Workspace,
)
from ..serializers import workspace_dict
from ..storage import ImageStorage
from ..validation import as_bool


def services() -> ApplicationServices:
    return current_app.extensions["imagegen_services"]


def read_reference_uploads(files: list[FileStorage]) -> list[tuple[str, bytes]]:
    runtime = services().settings.runtime()
    if len(files) > runtime.max_assets_per_workspace:
        raise ServiceError(
            f"单次最多上传 {runtime.max_assets_per_workspace} 张参考图", status_code=413
        )
    uploads = []
    total = 0
    for uploaded in files:
        name = uploaded.filename or "reference"
        if "\x00" in name:
            raise ServiceError("参考图文件名无效")
        content = uploaded.read(runtime.max_attachment_bytes + 1)
        if len(content) > runtime.max_attachment_bytes:
            raise ServiceError(f"单张垫图不能超过 {runtime.max_attachment_mb} MiB", status_code=413)
        total += len(content)
        if total > runtime.max_attachment_total_bytes:
            raise ServiceError(
                f"垫图合计不能超过 {runtime.max_attachment_total_mb} MiB", status_code=413
            )
        uploads.append((name, content))
    return uploads


def read_generation_mask(upload: FileStorage) -> bytes:
    content = upload.read(MAX_MASK_BYTES + 1)
    if len(content) > MAX_MASK_BYTES:
        raise ServiceError("蒙版不能超过 4 MiB", code="invalid_mask", status_code=422)
    return content


def channels() -> ChannelRegistry:
    return current_app.extensions["channel_registry"]


def chat_models() -> ChatModelRegistry:
    return current_app.extensions["chat_model_registry"]


def matting_models() -> MattingModelRegistry:
    return current_app.extensions["matting_model_registry"]


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
            Workspace.kind.not_in(INTERNAL_WORKSPACE_KINDS),
        )
    )
    if workspace is None:
        raise ServiceError("工作站不存在", status_code=404)
    return workspace


def accessible_item(item_id: str, *, image_test: bool = False) -> GenerationItem:
    if image_test and not current_user.is_admin:
        abort(404)
    kind_filter = (
        GenerationJob.kind == IMAGE_TEST_KIND
        if image_test
        else GenerationJob.kind != IMAGE_TEST_KIND
    )
    query = (
        select(GenerationItem)
        .options(selectinload(GenerationItem.job))
        .join(GenerationJob)
        .where(GenerationItem.id == item_id, kind_filter)
    )
    if not current_user.is_admin:
        query = query.where(
            GenerationItem.user_id == current_user.id,
            GenerationJob.in_interactive_workspace(),
        )
    item = db.session.scalar(query)
    if item is None:
        abort(404)
    return item


def json_body(*, max_bytes: int | None = None) -> dict[str, Any]:
    if max_bytes is not None and request.is_json:
        content = request.stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ServiceError(
                f"请求内容超过 {max_bytes // 1024} KiB 限制",
                code="payload_too_large",
                status_code=413,
            )
        try:
            data = current_app.json.loads(content)
        except (ValueError, RecursionError):
            data = None
    else:
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
