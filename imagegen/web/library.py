from __future__ import annotations

from flask import jsonify, request, send_file
from flask_login import current_user, login_required
from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import Asset, GenerationItem, Workspace
from ..serializers import library_image_dict, workspace_dict
from ..services.image_library import LIBRARY_PAGE_SIZE, MAX_LIBRARY_BYTES, MAX_LIBRARY_IMAGES
from . import web
from .shared import image_extension, json_body, owned_workspace, services, storage


@web.get("/api/library-images")
@login_required
def list_library_images():
    offset = _query_offset()
    limit = _query_limit()
    images, total = services().image_library.page(
        current_user.id,
        offset=offset,
        limit=limit,
    )
    return jsonify(
        images=[library_image_dict(image) for image in images],
        total=total,
        has_more=offset + len(images) < total,
        max_count=MAX_LIBRARY_IMAGES,
        max_bytes=MAX_LIBRARY_BYTES,
    )


@web.post("/api/library-images")
@login_required
def add_library_images():
    if request.mimetype == "multipart/form-data":
        uploads = _read_uploads()
    else:
        uploads = [_source_upload(json_body())]
    images, added_count = services().image_library.add(current_user.id, uploads)
    payload = {
        "images": [library_image_dict(image) for image in images],
        "added_count": added_count,
    }
    return jsonify(**payload), 201 if added_count else 200


@web.delete("/api/library-images/<image_id>")
@login_required
def delete_library_image(image_id: str):
    services().image_library.delete(current_user.id, image_id)
    return jsonify(ok=True)


@web.get("/media/library-images/<image_id>")
@login_required
def library_image_file(image_id: str):
    image = services().image_library.get(current_user.id, image_id)
    return send_file(storage().read(image.storage_path), mimetype=image.mime_type, conditional=True)


@web.get("/media/library-images/<image_id>/thumbnail")
@login_required
def library_image_thumbnail(image_id: str):
    image = services().image_library.get(current_user.id, image_id)
    if not image.thumbnail_path:
        return send_file(
            storage().read(image.storage_path),
            mimetype=image.mime_type,
            conditional=True,
        )
    return send_file(
        storage().read(image.thumbnail_path),
        mimetype="image/webp",
        conditional=True,
    )


@web.post("/api/workspaces/<workspace_id>/assets/from-library/<image_id>")
@login_required
def import_library_image(workspace_id: str, image_id: str):
    workspace = owned_workspace(workspace_id)
    image = services().image_library.get(current_user.id, image_id)
    existing = db.session.scalar(
        select(Asset).where(
            Asset.workspace_id == workspace.id,
            Asset.sha256 == image.sha256,
            Asset.deleted_at.is_(None),
        )
    )
    if existing is not None:
        return jsonify(asset=workspace_dict(workspace, [existing])["assets"][0])
    asset = services().workspaces.add_assets(
        workspace,
        [(image.original_name, storage().read_bytes(image.storage_path))],
    )[0]
    return jsonify(asset=workspace_dict(workspace, [asset])["assets"][0]), 201


def _read_uploads() -> list[tuple[str, bytes]]:
    runtime = services().settings.runtime()
    files = request.files.getlist("images")
    if len(files) > runtime.max_assets_per_workspace:
        raise ServiceError(
            f"单次最多导入 {runtime.max_assets_per_workspace} 张图片",
            status_code=413,
        )
    uploads: list[tuple[str, bytes]] = []
    total = 0
    for uploaded in files:
        content = uploaded.read(runtime.max_attachment_bytes + 1)
        if len(content) > runtime.max_attachment_bytes:
            raise ServiceError(f"单张图片不能超过 {runtime.max_attachment_mb} MiB", status_code=413)
        total += len(content)
        uploads.append((uploaded.filename or "image", content))
    if total > runtime.max_attachment_total_bytes:
        raise ServiceError(
            f"图片合计不能超过 {runtime.max_attachment_total_mb} MiB",
            status_code=413,
        )
    return uploads


def _source_upload(data: dict) -> tuple[str, bytes]:
    asset_id = str(data.get("asset_id", ""))
    item_id = str(data.get("generation_item_id", ""))
    if bool(asset_id) == bool(item_id):
        raise ServiceError("请选择一张工作站图片或生成结果")
    if asset_id:
        asset = db.session.scalar(
            select(Asset)
            .join(Workspace)
            .where(
                Asset.id == asset_id,
                Workspace.user_id == current_user.id,
                Asset.deleted_at.is_(None),
            )
        )
        if asset is None:
            raise ServiceError("工作站图片不存在", status_code=404)
        return asset.original_name, storage().read_bytes(asset.storage_path)

    item = db.session.scalar(
        select(GenerationItem).where(
            GenerationItem.id == item_id,
            GenerationItem.user_id == current_user.id,
        )
    )
    if item is None or not item.output_path:
        raise ServiceError("生成结果不存在", status_code=404)
    name = f"result_{item.id}.{image_extension(item.output_mime_type)}"
    return name, storage().read_bytes(item.output_path)


def _query_offset() -> int:
    try:
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError) as exc:
        raise ServiceError("图库分页参数无效") from exc
    if offset < 0:
        raise ServiceError("图库分页参数无效")
    return offset


def _query_limit() -> int:
    try:
        limit = int(request.args.get("limit", LIBRARY_PAGE_SIZE))
    except (TypeError, ValueError) as exc:
        raise ServiceError("图库分页参数无效") from exc
    if limit < 1:
        raise ServiceError("图库分页参数无效")
    return min(limit, LIBRARY_PAGE_SIZE)
