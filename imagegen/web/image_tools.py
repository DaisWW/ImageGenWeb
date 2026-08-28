from __future__ import annotations

import io
import zipfile

from flask import jsonify, request, send_file
from flask_login import current_user, login_required
from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import Asset
from ..serializers import background_removal_run_dict, library_image_dict, workspace_dict
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


@web.get("/api/background-removal-models")
@login_required
def background_removal_models():
    return jsonify(models=services().background_removal.public_models())


@web.get("/api/generation-items/<item_id>/background-removal")
@login_required
def generation_item_background_removal(item_id: str):
    item = accessible_item(item_id)
    run = services().background_removal.get_for_item(item.id, user_id=item.user_id)
    return jsonify(
        models=services().background_removal.public_models(),
        run=background_removal_run_dict(run),
    )


@web.post("/api/generation-items/<item_id>/background-removal")
@login_required
def submit_generation_item_background_removal(item_id: str):
    item = accessible_item(item_id)
    data = json_body()
    raw_model_ids = data.get("model_ids")
    if not isinstance(raw_model_ids, list):
        raise ServiceError("透明化模型列表格式无效")
    run = services().background_removal.submit(
        item.id,
        user_id=item.user_id,
        model_ids=tuple(str(model_id).strip() for model_id in raw_model_ids),
    )
    return jsonify(run=background_removal_run_dict(run)), 202


@web.post("/api/background-removal-results/<result_id>/select")
@login_required
def select_background_removal_result(result_id: str):
    run = services().background_removal.select(result_id, user_id=current_user.id)
    return jsonify(run=background_removal_run_dict(run))


@web.get("/api/background-removal-runs/<run_id>/download")
@login_required
def download_background_removal_run(run_id: str):
    run = services().background_removal.get_run(run_id, user_id=current_user.id)
    completed = [result for result in run.results if result.output_path]
    if not completed:
        raise ServiceError("暂无可下载的透明化结果", status_code=409)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for result in completed:
            bundle.writestr(
                f"{result.model_id}_{result.id}.png",
                storage().read_bytes(result.output_path),
            )
    archive.seek(0)
    return send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"image_{run.source_item_id}_background_removals.zip",
    )


@web.get("/media/background-removal-results/<result_id>")
@login_required
def background_removal_file(result_id: str):
    result = services().background_removal.get_result(result_id, user_id=current_user.id)
    if not result.output_path:
        raise ServiceError("透明化结果不存在", status_code=404)
    return send_file(
        storage().read(result.output_path),
        mimetype=result.output_mime_type or "image/png",
        as_attachment=request.args.get("download") == "1",
        download_name=f"image_{result.run.source_item_id}_{result.model_id}.png",
        conditional=True,
    )


@web.get("/media/background-removal-results/<result_id>/thumbnail")
@login_required
def background_removal_thumbnail(result_id: str):
    result = services().background_removal.get_result(result_id, user_id=current_user.id)
    if not result.thumbnail_path:
        raise ServiceError("透明化缩略图不存在", status_code=404)
    return send_file(
        storage().read(result.thumbnail_path),
        mimetype="image/webp",
        conditional=True,
    )


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
