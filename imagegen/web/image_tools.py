from __future__ import annotations

import io
import zipfile
from pathlib import Path

from flask import current_app, jsonify, send_file
from flask_login import current_user, login_required
from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..integrations.matting import image_has_real_alpha
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
    asset, created = _generation_item_asset(item, workspace)
    return jsonify(asset=workspace_dict(workspace, [asset])["assets"][0]), (201 if created else 200)


@web.post("/api/generation-items/<item_id>/series-anchor")
@login_required
def set_generation_series_anchor(item_id: str):
    item = accessible_item(item_id)
    if not item.output_path:
        raise ServiceError("生成结果不存在", status_code=404)
    workspace = owned_workspace(item.job.workspace_id)
    workflow = item.job.workflow if isinstance(item.job.workflow, dict) else {}
    contract = workflow.get("series_contract")
    if not isinstance(contract, dict) or not contract:
        raise ServiceError(
            "该结果没有可复用的系列制作契约，请先使用 AI 整理提示词", status_code=409
        )
    asset, _created = _generation_item_asset(item, workspace)
    workspace = services().workspaces.set_series_anchor(
        workspace,
        asset_id=asset.id,
        source_item_id=item.id,
        contract=contract,
    )
    return jsonify(
        asset=workspace_dict(workspace, [asset])["assets"][0],
        workspace=workspace_dict(workspace),
    ), 201


def _generation_item_asset(item, workspace):
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
        return existing, False
    assets = services().workspaces.add_assets(
        workspace,
        [
            (
                asset_name,
                storage().read_bytes(item.output_path),
            )
        ],
    )
    return assets[0], True


@web.post("/api/generation-items/<item_id>/review")
@login_required
def review_generation_item(item_id: str):
    item = accessible_item(item_id)
    data = json_body()
    review = services().conversations.review_generation_item(
        item,
        model_id=str(data.get("model_id", "")),
    )
    return jsonify(review=review)


@web.post("/api/generation-items/<item_id>/slice-analysis")
@login_required
def analyze_generation_item_slices(item_id: str):
    item = accessible_item(item_id)
    if not item.output_path:
        raise ServiceError("生成结果不存在", status_code=404)
    analysis = analyze_image(
        storage().read(item.output_path), prompt=item.prompt or item.job.prompt
    )
    return jsonify(analysis=analysis)


@web.post("/api/generation-items/<item_id>/matting")
@login_required
def matte_generation_item(item_id: str):
    item = accessible_item(item_id)
    if not item.output_path:
        raise ServiceError("生成结果不存在", status_code=404)
    original = storage().read_bytes(item.output_path)
    if image_has_real_alpha(original):
        raise ServiceError(
            "该结果已包含真实透明通道，无需再抠图",
            code="matting_already_transparent",
            status_code=409,
        )
    matted = _lucida_client().remove_background(
        original,
        filename=f"image_{item.id}.{image_extension(item.output_mime_type)}",
    )
    return send_file(
        io.BytesIO(matted),
        mimetype="image/png",
        as_attachment=True,
        download_name=f"image_{item.id}_lucida.png",
    )


@web.post("/api/generation-items/<item_id>/slice-export")
@login_required
def export_generation_item_slices(item_id: str):
    item = accessible_item(item_id)
    if not item.output_path:
        raise ServiceError("生成结果不存在", status_code=404)
    data = json_body()
    action = str(data.get("action", "")).strip().lower()
    if action not in {"download", "library", "reference", "matting"}:
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

    if action == "matting":
        client = _lucida_client()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, content in crops:
                if image_has_real_alpha(content):
                    raise ServiceError(
                        f"切片 {name} 已包含真实透明通道，无需再抠图",
                        code="matting_already_transparent",
                        status_code=409,
                    )
                matted = client.remove_background(content, filename=name)
                stem = Path(name).stem
                bundle.writestr(f"{stem}_lucida.png", matted)
        archive.seek(0)
        return send_file(
            archive,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"image_{item.id}_slices_lucida.zip",
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


def _lucida_client():
    client = current_app.extensions.get("lucida_matting_client")
    if client is None:
        raise ServiceError(
            "Lucida 抠图服务未配置（请设置 LUCIDA_MATTING_URL）",
            code="matting_unavailable",
            status_code=503,
        )
    return client
