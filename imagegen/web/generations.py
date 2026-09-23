from __future__ import annotations

import json

from flask import abort, jsonify, request, send_file
from flask_login import current_user, login_required
from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..image_masks import MAX_MASK_BYTES
from ..models import Asset, Workspace
from ..serializers import job_dict, job_status_dict
from ..services import GenerationMaskInput, GenerationWorkflow, SubmitGeneration
from ..services.common import canvas_request_conflicts
from ..services.generations.contracts import CANVAS_RESOLUTIONS
from ..services.generations.planning import GenerationPlan, normalize_generation_strategy
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
    )


def _generation_submission_body() -> tuple[dict, GenerationMaskInput | None]:
    if request.mimetype != "multipart/form-data":
        data = json_body()
        if str(data.get("mask_target_asset_id", "")).strip():
            raise ServiceError(
                "局部重绘请求缺少 PNG 蒙版",
                code="invalid_mask",
                status_code=422,
            )
        return data, None

    raw_payload = request.form.get("payload", "")
    try:
        data = json.loads(raw_payload)
    except (TypeError, ValueError) as exc:
        raise ServiceError("局部重绘参数必须是有效 JSON") from exc
    if not isinstance(data, dict):
        raise ServiceError("局部重绘参数必须是 JSON 对象")
    upload = request.files.get("mask")
    if upload is None:
        raise ServiceError(
            "局部重绘请求缺少 PNG 蒙版",
            code="invalid_mask",
            status_code=422,
        )
    target_asset_id = str(data.get("mask_target_asset_id", "")).strip().lower()
    if not target_asset_id:
        raise ServiceError(
            "局部重绘请求缺少目标图片",
            code="invalid_mask",
            status_code=422,
        )
    content = upload.stream.read(MAX_MASK_BYTES + 1)
    if len(content) > MAX_MASK_BYTES:
        raise ServiceError(
            "蒙版不能超过 4 MiB",
            code="invalid_mask",
            status_code=422,
        )
    return data, GenerationMaskInput(target_asset_id=target_asset_id, content=content)


@web.post("/api/generations")
@login_required
def submit_generation():
    data, mask = _generation_submission_body()
    workspace = owned_workspace(str(data.get("workspace_id", "")))
    reference_ids = data.get("reference_ids", [])
    if not isinstance(reference_ids, list):
        raise ServiceError("垫图参数无效")
    try:
        compression = int(data.get("compression", 90))
        batch_count = int(data.get("batch_count", 1))
    except (TypeError, ValueError) as exc:
        raise ServiceError("生成数量或压缩质量无效") from exc
    prompt = str(data.get("prompt", ""))
    ordered_reference_ids = tuple(str(item).strip().lower() for item in reference_ids)
    application_services = services()
    runtime = application_services.settings.runtime()
    if not 1 <= batch_count <= runtime.max_batch_images:
        raise ServiceError(f"单批生成张数必须在 1 到 {runtime.max_batch_images} 之间")
    strategy = normalize_generation_strategy(
        data.get(
            "generation_strategy",
            (workspace.settings or {}).get("generation_strategy", "sample"),
        )
    )
    if mask is not None and strategy != "sample":
        raise ServiceError(
            "局部重绘当前仅支持同提示词抽样",
            code="mask_strategy_invalid",
            status_code=422,
        )
    mode = "img2img" if ordered_reference_ids else "text2img"
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
    canvas_resolution = str(data.get("canvas_resolution", "")).strip().lower()
    if canvas_resolution and canvas_resolution not in CANVAS_RESOLUTIONS:
        raise ServiceError("画幅冲突处理方式无效")
    requested_size = str(data.get("size", "1024x1024"))
    if draft and canvas_request_conflicts(draft.get("canvas_request"), requested_size):
        if canvas_resolution == "conversation":
            raise ServiceError(
                "请先将尺寸改为对话要求的画幅",
                code="prompt_canvas_conflict",
                status_code=409,
            )
        if canvas_resolution != "panel":
            raise ServiceError(
                "对话要求的画幅与当前尺寸不一致，请确认后重试",
                code="prompt_canvas_conflict",
                status_code=409,
            )
    plan = GenerationPlan.build(
        strategy=strategy,
        prompt=prompt,
        count=batch_count,
        draft=draft,
        max_prompt_characters=runtime.max_prompt_characters,
    )
    workflow = GenerationWorkflow.build(
        stage=str(data.get("generation_stage", "final")),
        prompt_draft_id=draft_id,
        draft=draft,
        creative_direction_id=str(data.get("creative_direction_id", "auto")),
        canvas_resolution=canvas_resolution,
        plan_metadata=plan.metadata,
    )
    generation_request = SubmitGeneration(
        channel_id=str(data.get("channel_id", "")),
        model=str(data.get("model", "")),
        mode=mode,
        prompt=prompt,
        size=requested_size,
        output_format=str(data.get("output_format", "png")),
        compression=compression,
        batch_count=batch_count,
        reference_ids=ordered_reference_ids,
        item_prompts=plan.prompts,
        quality=str(data.get("quality", "auto")).strip().lower(),
        workflow=workflow.metadata,
        transparent_background=json_bool(data.get("transparent_background", False)),
        moderation=str(data.get("moderation", "auto")).strip().lower(),
        mask=mask,
    )
    generation_service = application_services.generations
    operation_id = str(data.get("operation_id", "")).strip().lower()
    with application_services.conversations.generation_submission(
        workspace,
        operation_id=operation_id,
    ) as operation:
        job = generation_service.submit(current_user.id, workspace, generation_request)
        if operation.cancel_event.is_set():
            generation_service.cancel(job.id, user_id=current_user.id)
            operation.ensure_active()
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
def retry_generation(job_id: str):
    generation_service = services().generations
    job = generation_service.retry(job_id, user_id=current_user.id)
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
