from __future__ import annotations

import io
import zipfile

from flask import jsonify, send_file
from flask_login import current_user, login_required
from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import Asset
from ..serializers import library_image_dict, workspace_dict
from ..services.image_slicing import (
    analyze_image,
    crop_pngs,
    image_size,
    validate_boxes,
)
from . import web
from .shared import (
    accessible_item,
    image_extension,
    json_body,
    owned_workspace,
    services,
    storage,
)


@web.post("/api/generation-items/<item_id>/reference")
@login_required
def reuse_generation_item(item_id: str):
    item = accessible_item(item_id)
    if not item.output_path:
        raise ServiceError("生成结果不存在", status_code=404)
    workspace = owned_workspace(item.job.workspace_id)
    extension = image_extension(item.output_mime_type)
    asset_name = f"result_{item.id}.{extension}"
    existing = db.session.scalar(
        select(Asset)
        .where(
            Asset.workspace_id == workspace.id,
            Asset.original_name == asset_name,
            Asset.deleted_at.is_(None),
        )
        .order_by(Asset.created_at)
        .limit(1)
    )
    if existing is not None:
        return jsonify(asset=workspace_dict(workspace, [existing])["assets"][0])
    assets = services().workspaces.add_assets(
        workspace,
        [
            (
                asset_name,
                storage().read_bytes(item.output_path),
            )
        ],
    )
    return jsonify(asset=workspace_dict(workspace, assets)["assets"][0]), 201


@web.post("/api/generation-items/<item_id>/review")
@login_required
def review_generation_item(item_id: str):
    item = accessible_item(item_id)
    review = services().conversations.review_generation_item(
        item,
        model_id=str(json_body().get("model_id", "")),
    )
    return jsonify(review=review)


@web.post("/api/generation-items/<item_id>/slice-analysis")
@login_required
def analyze_generation_item_slices(item_id: str):
    item = accessible_item(item_id)
    if not item.output_path:
        raise ServiceError("生成结果不存在", status_code=404)
    analysis = analyze_image(storage().read(item.output_path), prompt=item.job.prompt)
    return jsonify(analysis=analysis)


@web.post("/api/generation-items/<item_id>/slice-export")
@login_required
def export_generation_item_slices(item_id: str):
    item = accessible_item(item_id)
    if not item.output_path:
        raise ServiceError("生成结果不存在", status_code=404)
    data = json_body()
    action = str(data.get("action", "")).strip().lower()
    if action not in {"download", "library", "reference"}:
        raise ServiceError("切图操作无效")
    path = storage().read(item.output_path)
    width, height = image_size(path)
    try:
        boxes = validate_boxes(data.get("boxes"), width=width, height=height)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    if action == "reference" and len(boxes) != 1:
        raise ServiceError("继续生成时只能选择一个切片")
    crops = crop_pngs(path, boxes)

    if action == "download":
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, content in crops:
                bundle.writestr(name, content)
        archive.seek(0)
        return send_file(
            archive,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"image_{item.id}_slices.zip",
        )

    if action == "library":
        images, added_count = services().image_library.add(current_user.id, crops)
        return jsonify(
            images=[library_image_dict(image) for image in images],
            added_count=added_count,
        ), (201 if added_count else 200)

    workspace = owned_workspace(item.job.workspace_id)
    asset = services().workspaces.add_assets(workspace, crops)[0]
    return jsonify(asset=workspace_dict(workspace, [asset])["assets"][0]), 201
