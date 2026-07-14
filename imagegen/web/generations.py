from __future__ import annotations

from flask import abort, jsonify, request, send_file
from flask_login import current_user, login_required
from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import Asset, Workspace
from ..serializers import job_dict, workspace_dict
from ..services import SubmitGeneration
from . import web
from .shared import (
    accessible_item,
    channels,
    image_extension,
    json_body,
    json_bool,
    owned_workspace,
    query_limit,
    services,
    storage,
)


@web.post("/api/generations")
@login_required
def submit_generation():
    data = json_body()
    workspace = owned_workspace(str(data.get("workspace_id", "")))
    services().conversations.ensure_idle(workspace.id)
    reference_ids = data.get("reference_ids", [])
    if not isinstance(reference_ids, list):
        raise ServiceError("垫图参数无效")
    try:
        compression = int(data.get("compression", 90))
        batch_count = int(data.get("batch_count", 1))
    except (TypeError, ValueError) as exc:
        raise ServiceError("生成数量或压缩质量无效") from exc
    generation_service = services().generations
    job = generation_service.submit(
        current_user.id,
        workspace,
        SubmitGeneration(
            channel_id=str(data.get("channel_id", "")),
            model=str(data.get("model", "")),
            mode=str(data.get("mode", "text2img")),
            prompt=str(data.get("prompt", "")),
            size=str(data.get("size", "1024x1024")),
            quality=str(data.get("quality", "medium")),
            output_format=str(data.get("output_format", "png")),
            compression=compression,
            batch_count=batch_count,
            reference_ids=tuple(str(item) for item in reference_ids),
            transparent_background=json_bool(data.get("transparent_background", False)),
        ),
    )
    positions = generation_service.queue_positions()
    return jsonify(
        job=job_dict(
            job,
            channels(),
            queue_position=positions.get(job.id),
            queue_total=len(positions),
        )
    ), 202


@web.get("/api/generations")
@login_required
def list_generations():
    workspace_id = request.args.get("workspace_id", "")
    if workspace_id:
        owned_workspace(workspace_id)
    generation_service = services().generations
    jobs = generation_service.list_jobs(
        user_id=current_user.id,
        workspace_id=workspace_id or None,
        limit=query_limit(),
    )
    positions = generation_service.queue_positions()
    return jsonify(
        jobs=[
            job_dict(
                job,
                channels(),
                queue_position=positions.get(job.id),
                queue_total=len(positions),
            )
            for job in jobs
        ],
        queue_total=len(positions),
    )


@web.get("/api/generations/<job_id>")
@login_required
def get_generation(job_id: str):
    generation_service = services().generations
    job = generation_service.get_job(job_id, user_id=current_user.id)
    positions = generation_service.queue_positions()
    return jsonify(
        job=job_dict(
            job,
            channels(),
            queue_position=positions.get(job.id),
            queue_total=len(positions),
        )
    )


@web.post("/api/generations/<job_id>/cancel")
@login_required
def cancel_generation(job_id: str):
    generation_service = services().generations
    job = generation_service.cancel(job_id, user_id=current_user.id)
    positions = generation_service.queue_positions()
    return jsonify(
        job=job_dict(
            job,
            channels(),
            queue_position=positions.get(job.id),
            queue_total=len(positions),
        )
    )


@web.get("/media/assets/<asset_id>")
@login_required
def asset_file(asset_id: str):
    query = select(Asset).join(Workspace).where(Asset.id == asset_id)
    if not current_user.is_admin:
        query = query.where(Workspace.user_id == current_user.id)
    asset = db.session.scalar(query)
    if asset is None:
        abort(404)
    return send_file(
        storage().read(asset.storage_path),
        mimetype=asset.mime_type,
        conditional=True,
    )


@web.get("/media/outputs/<item_id>")
@login_required
def output_file(item_id: str):
    item = accessible_item(item_id)
    if not item.output_path:
        abort(404)
    return send_file(
        storage().read(item.output_path),
        mimetype=item.output_mime_type,
        as_attachment=request.args.get("download") == "1",
        download_name=(f"image_{item.id}.{image_extension(item.output_mime_type)}"),
        conditional=True,
    )


@web.get("/media/thumbnails/<item_id>")
@login_required
def output_thumbnail(item_id: str):
    item = accessible_item(item_id)
    if not item.thumbnail_path:
        abort(404)
    return send_file(
        storage().read(item.thumbnail_path),
        mimetype="image/webp",
        conditional=True,
    )


@web.post("/api/generation-items/<item_id>/reference")
@login_required
def reuse_generation_item(item_id: str):
    item = accessible_item(item_id)
    if not item.output_path:
        raise ServiceError("生成结果不存在", status_code=404)
    workspace = owned_workspace(item.job.workspace_id)
    extension = image_extension(item.output_mime_type)
    assets = services().workspaces.add_assets(
        workspace,
        [
            (
                f"result_{item.id}.{extension}",
                storage().read_bytes(item.output_path),
            )
        ],
    )
    return jsonify(asset=workspace_dict(workspace, assets)["assets"][0]), 201
