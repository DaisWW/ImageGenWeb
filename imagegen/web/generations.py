from __future__ import annotations

from flask import abort, jsonify, request, send_file
from flask_login import current_user, login_required
from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import Asset, Workspace
from ..serializers import job_dict, job_status_dict
from ..services import GenerationWorkflow, SubmitGeneration
from ..storage import StorageError
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


def _queue_positions_for(generation_service, jobs) -> dict[str, int]:
    return (
        generation_service.queue_positions() if any(job.status == "queued" for job in jobs) else {}
    )


def _job_payload(generation_service, job):
    positions = _queue_positions_for(generation_service, (job,))
    return job_dict(
        job,
        channels(),
        queue_position=positions.get(job.id),
        queue_total=len(positions),
        generation_concurrency=current_user.generation_concurrency,
    )


@web.post("/api/generations")
@login_required
def submit_generation():
    data = json_body()
    workspace = owned_workspace(str(data.get("workspace_id", "")))
    reference_ids = data.get("reference_ids", [])
    if not isinstance(reference_ids, list):
        raise ServiceError("垫图参数无效")
    try:
        compression = int(data.get("compression", 90))
        batch_count = int(data.get("batch_count", 1))
        frame_count = int(data.get("animation_frame_count", data.get("frame_count", 8)))
        animation_fps = int(data.get("animation_fps", 8))
    except (TypeError, ValueError) as exc:
        raise ServiceError("生成数量、帧率或压缩质量无效") from exc
    mode = str(data.get("mode", "text2img"))
    prompt = str(data.get("prompt", ""))
    ordered_reference_ids = tuple(str(item) for item in reference_ids)
    application_services = services()
    draft_id = str(data.get("prompt_draft_id", "")).strip()
    draft = None
    if draft_id:
        draft = application_services.conversations.validate_generation_draft(
            workspace,
            draft_id=draft_id,
            prompt=prompt,
            mode=mode,
            reference_ids=ordered_reference_ids if mode == "img2img" else (),
        )
    workflow = GenerationWorkflow.build(
        stage=str(data.get("generation_stage", "draft")),
        prompt_draft_id=draft_id,
        draft=draft,
        creative_direction_id=str(data.get("creative_direction_id", "auto")),
    )
    generation_request = SubmitGeneration(
        channel_id=str(data.get("channel_id", "")),
        model=str(data.get("model", "")),
        mode=mode,
        prompt=prompt,
        size=str(data.get("size", "1024x1024")),
        output_format=str(data.get("output_format", "png")),
        compression=compression,
        batch_count=batch_count,
        reference_ids=ordered_reference_ids,
        quality=workflow.quality,
        workflow=workflow.metadata,
        transparent_background=json_bool(data.get("transparent_background", False)),
        frame_count=frame_count,
        animation_fps=animation_fps,
        animation_loop=json_bool(data.get("animation_loop", True)),
        animation_format=str(data.get("animation_format", "webp")).lower(),
    )
    generation_service = application_services.generations
    with application_services.conversations.generation_submission(workspace):
        job = generation_service.submit(current_user.id, workspace, generation_request)
    return jsonify(job=_job_payload(generation_service, job)), 202


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
    positions = _queue_positions_for(generation_service, jobs)
    return jsonify(
        jobs=[
            job_dict(
                job,
                channels(),
                queue_position=positions.get(job.id),
                queue_total=len(positions),
                generation_concurrency=current_user.generation_concurrency,
            )
            for job in jobs
        ],
        queue_total=len(positions),
    )


@web.get("/api/generations/active")
@login_required
def list_active_generations():
    generation_service = services().generations
    jobs = generation_service.list_active_jobs(current_user.id)
    positions = _queue_positions_for(generation_service, jobs)
    return jsonify(
        jobs=[
            job_status_dict(
                job,
                channels(),
                queue_position=positions.get(job.id),
                queue_total=len(positions),
                generation_concurrency=current_user.generation_concurrency,
            )
            for job in jobs
        ]
    )


@web.get("/api/generations/<job_id>")
@login_required
def get_generation(job_id: str):
    generation_service = services().generations
    job = generation_service.get_job(job_id, user_id=current_user.id)
    return jsonify(job=_job_payload(generation_service, job))


@web.post("/api/generations/<job_id>/cancel")
@login_required
def cancel_generation(job_id: str):
    generation_service = services().generations
    job = generation_service.cancel(job_id, user_id=current_user.id)
    return jsonify(job=_job_payload(generation_service, job))


@web.post("/api/generations/<job_id>/retry")
@login_required
def retry_animation_generation(job_id: str):
    application_services = services()
    generation_service = application_services.generations
    existing = generation_service.get_job(job_id, user_id=current_user.id)
    workspace = owned_workspace(existing.workspace_id)
    with application_services.conversations.generation_submission(workspace):
        job = generation_service.retry_animation(job_id, user_id=current_user.id)
    return jsonify(job=_job_payload(generation_service, job)), 202


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


@web.get("/media/animations/<job_id>")
@login_required
def animation_file(job_id: str):
    job = services().generations.get_job(
        job_id,
        user_id=current_user.id,
        admin=current_user.is_admin,
    )
    if job.kind != "animation" or job.status != "succeeded":
        abort(404)
    frame_paths = [item.output_path for item in job.items]
    if not all(frame_paths):
        abort(404)
    try:
        animation = storage().save_animation(
            user_id=job.user_id,
            workspace_id=job.workspace_id,
            job_id=job.id,
            frame_paths=frame_paths,
            output_format=job.animation_format,
            fps=job.animation_fps,
            loop=job.animation_loop,
        )
    except StorageError as exc:
        raise ServiceError(str(exc), code="animation_export_failed", status_code=409) from exc
    return send_file(
        storage().read(animation.relative_path),
        mimetype=animation.mime_type,
        as_attachment=request.args.get("download") == "1",
        download_name=f"animation_{job.id}.{animation.extension}",
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
