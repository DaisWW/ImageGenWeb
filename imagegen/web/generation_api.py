from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from flask import (
    Blueprint,
    g,
    jsonify,
    make_response,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from ..config.channels import AUTO_CHANNEL_ID
from ..errors import ServiceError
from ..extensions import csrf
from ..image_masks import MAX_MASK_BYTES
from ..models import GenerationJob
from ..serializers import job_status_dict
from ..services import GenerationWorkflow, SubmitGeneration
from ..services.generations.planning import normalize_generation_strategy
from . import web
from .shared import channels, image_extension, json_body, json_bool, services, storage

api = Blueprint("generation_api", __name__, url_prefix="/api/v1")
SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "snow-image-api"
# Allow a 12,000-character prompt encoded as JSON Unicode escapes plus metadata.
MAX_GENERATION_REQUEST_BYTES = 192 * 1024


@web.get("/account/generation-api")
@login_required
def generation_api_page():
    enabled, token = services().generation_api_keys.key_for_display(current_user.id)
    response = make_response(
        render_template(
            "pages/generation-api.html",
            key_enabled=enabled,
            api_key=token,
            base_url=request.url_root.rstrip("/"),
            can_package_key=_can_package_key(),
        )
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@web.route("/account/generation-api/key", methods=["POST", "DELETE"])
def generation_api_key():
    if not current_user.is_authenticated:
        raise ServiceError("请先登录", code="login_required", status_code=401)
    keys = services().generation_api_keys
    if request.method == "DELETE":
        keys.revoke_key(current_user.id)
        return "", 204

    token = keys.issue_key(current_user.id)
    response = jsonify(api_key=token)
    response.headers["Cache-Control"] = "no-store"
    return response


def _can_package_key() -> bool:
    return request.is_secure or urlsplit(request.url_root).hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


@web.route("/account/generation-api/skill", methods=["GET", "POST"])
@login_required
def generation_api_skill():
    connection = None
    if request.method == "POST":
        if not _can_package_key():
            raise ServiceError("请通过 HTTPS 下载免配置 Skill")
        token = request.form.get("api_key", "")
        key, _ = services().generation_api_keys.authenticate(token)
        if key.user_id != current_user.id:
            raise ServiceError("只能打包当前账号的密钥", status_code=403)
        connection = {"base_url": request.url_root.rstrip("/"), "api_key": token}

    bundle = BytesIO()
    with ZipFile(bundle, "w", compression=ZIP_DEFLATED) as archive:
        for entry in (
            "SKILL.md",
            "references/api.md",
            "references/request.json",
            "scripts/generate.py",
        ):
            archive.write(SKILL_DIR / entry, f"{SKILL_DIR.name}/{entry}")
        if connection is not None:
            archive.writestr(f"{SKILL_DIR.name}/connection.json", json.dumps(connection))
    bundle.seek(0)
    suffix = "-configured" if connection is not None else ""
    response = send_file(
        bundle,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{SKILL_DIR.name}{suffix}.zip",
    )
    if connection is not None:
        response.headers["Cache-Control"] = "private, no-store"
    return response


@api.before_request
def authenticate_generation_api():
    authorization = request.authorization
    token = authorization.token if authorization and authorization.type == "bearer" else None
    key, balance = services().generation_api_keys.authenticate(token or "")
    g.generation_api_key = key
    g.generation_api_user_id = key.user_id
    g.generation_api_balance = balance


@api.after_request
def generation_api_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    if response.status_code == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="Snow AI Studio"'
    return response


@api.get("/config")
def config():
    runtime = services().settings.runtime()
    registry = channels()
    return jsonify(
        available_balance_rmb=format(g.generation_api_balance, ".4f"),
        channels=[
            channel.public_dict()
            for channel in registry.list(include_disabled=False)
            if channel.configured
        ],
        limits={
            "max_batch_images": runtime.max_batch_images,
            "max_prompt_characters": runtime.max_prompt_characters,
            "max_reference_images": runtime.max_assets_per_workspace,
            "max_reference_image_mb": runtime.max_attachment_mb,
            "max_reference_total_mb": runtime.max_attachment_total_mb,
            "max_mask_bytes": MAX_MASK_BYTES,
        },
        history_retention_days=registry.queue.history_retention_days,
    )


@api.post("/generations")
@csrf.exempt
def submit():
    request_id = _uuid_hex(
        request.headers.get("Idempotency-Key"),
        "Idempotency-Key 必须是 UUID",
        code="invalid_request_id",
    )
    data, reference_uploads, mask_content = _generation_submission_body()
    generation = _parse_generation(data, has_references=bool(reference_uploads))
    job, created = services().generation_api.submit(
        g.generation_api_key,
        request_id,
        generation,
        reference_uploads=reference_uploads,
        mask_content=mask_content,
    )
    status_url = url_for("generation_api.status", job_id=job.id)
    return jsonify(job=_job_payload(job)), 202 if created else 200, {"Location": status_url}


@api.get("/generations/<job_id>")
def status(job_id: str):
    job = services().generation_api.get_job(g.generation_api_key, job_id)
    return jsonify(job=_job_payload(job))


@api.get("/generations/<job_id>/images/<item_id>")
def download(job_id: str, item_id: str):
    output_path, mime_type = services().generation_api.get_image(
        g.generation_api_key, job_id, item_id
    )
    try:
        return send_file(
            storage().read(output_path),
            mimetype=mime_type,
            as_attachment=True,
            download_name=f"image_{item_id}.{image_extension(mime_type)}",
            conditional=True,
        )
    except FileNotFoundError as exc:
        raise ServiceError("图片不存在", code="image_not_found", status_code=404) from exc


def _uuid_hex(value: object, message: str, *, code: str = "invalid_request") -> str:
    try:
        return UUID(value if isinstance(value, str) else "").hex
    except ValueError as exc:
        raise ServiceError(message, code=code) from exc


def _generation_submission_body() -> tuple[dict, tuple[tuple[str, bytes], ...], bytes | None]:
    if request.mimetype != "multipart/form-data":
        return json_body(max_bytes=MAX_GENERATION_REQUEST_BYTES), (), None

    if request.form.keys() - {"payload"} or request.files.keys() - {"references", "mask"}:
        raise ServiceError("上传请求包含不支持的字段")
    raw_payload = request.form.get("payload", "")
    if len(raw_payload.encode("utf-8")) > MAX_GENERATION_REQUEST_BYTES:
        raise ServiceError("生成参数超过 192 KiB 限制", code="payload_too_large", status_code=413)
    try:
        data = json.loads(raw_payload)
    except (ValueError, RecursionError) as exc:
        raise ServiceError("请求内容必须是有效 JSON") from exc
    if not isinstance(data, dict):
        raise ServiceError("请求内容必须是 JSON 对象")

    runtime = services().settings.runtime()
    uploads = request.files.getlist("references")
    if len(uploads) > runtime.max_assets_per_workspace:
        raise ServiceError(
            f"单次最多上传 {runtime.max_assets_per_workspace} 张垫图", status_code=413
        )
    reference_uploads = []
    total_bytes = 0
    for upload in uploads:
        content = upload.stream.read(runtime.max_attachment_bytes + 1)
        if len(content) > runtime.max_attachment_bytes:
            raise ServiceError(f"单张垫图不能超过 {runtime.max_attachment_mb} MiB", status_code=413)
        total_bytes += len(content)
        reference_uploads.append((upload.filename or "reference", content))
    if total_bytes > runtime.max_attachment_total_bytes:
        raise ServiceError(
            f"垫图合计不能超过 {runtime.max_attachment_total_mb} MiB", status_code=413
        )

    masks = request.files.getlist("mask")
    if len(masks) > 1:
        raise ServiceError("局部重绘只能上传一张蒙版", code="invalid_mask", status_code=422)
    mask_content = None
    if masks:
        if not reference_uploads:
            raise ServiceError("局部重绘必须同时上传一张原图", code="invalid_mask", status_code=422)
        mask_content = masks[0].stream.read(MAX_MASK_BYTES + 1)
        if len(mask_content) > MAX_MASK_BYTES:
            raise ServiceError("蒙版不能超过 4 MiB", code="invalid_mask", status_code=422)

    return data, tuple(reference_uploads), mask_content


def _parse_generation(data: dict, *, has_references: bool = False) -> SubmitGeneration:
    defaults = {
        "channel_id": "",
        "model": "",
        "prompt": "",
        "size": "auto",
        "quality": "auto",
        "output_format": "png",
        "moderation": "auto",
    }
    optional = {
        "batch_count",
        "compression",
        "transparent_background",
        "generation_stage",
        "generation_strategy",
        "creative_direction_id",
        "item_prompts",
    }
    if data.keys() - (defaults.keys() | optional):
        raise ServiceError("请求包含不支持的字段")
    values = {}
    for name, default in defaults.items():
        value = data.get(name, default)
        if not isinstance(value, str) or "\x00" in value:
            raise ServiceError(f"{name} 必须是有效文本")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ServiceError(f"{name} 必须是有效文本") from exc
        values[name] = value.strip()
    if not values["channel_id"] or values["channel_id"] == AUTO_CHANNEL_ID:
        raise ServiceError("请选择一个生图渠道")
    for name in ("quality", "output_format", "moderation"):
        values[name] = values[name].lower()
    batch_count = data.get("batch_count", 1)
    compression = data.get("compression", 90)
    if type(batch_count) is not int or type(compression) is not int:
        raise ServiceError("生成数量和压缩质量必须是整数")

    raw_item_prompts = data.get("item_prompts", [])
    if not isinstance(raw_item_prompts, list) or any(
        not isinstance(prompt, str) or "\x00" in prompt for prompt in raw_item_prompts
    ):
        raise ServiceError("逐张提示词参数无效")
    try:
        item_prompts = tuple(prompt.strip() for prompt in raw_item_prompts)
        for prompt in item_prompts:
            prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ServiceError("逐张提示词参数无效") from exc
    strategy = normalize_generation_strategy(data.get("generation_strategy", "sample"))
    if strategy == "explore" and not item_prompts:
        raise ServiceError(
            "探索方案需要逐张提示词，请由 AI 提供 item_prompts",
            code="generation_prompts_required",
            status_code=422,
        )
    stage = data.get("generation_stage", "final")
    creative_direction_id = data.get("creative_direction_id", "auto")
    if not isinstance(stage, str) or not isinstance(creative_direction_id, str):
        raise ServiceError("生成阶段或创作方向参数无效")
    workflow = GenerationWorkflow.build(
        stage=stage,
        prompt_draft_id="",
        draft=None,
        creative_direction_id=creative_direction_id,
        plan_metadata={"generation_strategy": strategy},
    )

    return SubmitGeneration(
        **values,
        mode="img2img" if has_references else "text2img",
        batch_count=batch_count,
        compression=compression,
        reference_ids=(),
        item_prompts=item_prompts,
        workflow=workflow.metadata,
        transparent_background=json_bool(data.get("transparent_background", False)),
    )


def _job_payload(job: GenerationJob) -> dict:
    status = job_status_dict(job, channels())
    return {
        "id": job.id,
        "request_id": job.api_request_id,
        "status": job.status,
        "progress_percent": status["progress_percent"],
        "estimated_end_at": status["estimated_end_at"],
        "channel_id": job.channel_id,
        "model": job.model,
        "size": job.size,
        "requested_count": job.requested_count,
        "reserved_rmb": format(job.reserved_rmb, ".4f"),
        "charged_rmb": format(job.charged_rmb, ".4f"),
        "items": [
            {
                "id": item.id,
                "status": item.status,
                "error_code": item.error_code or None,
                "width": item.output_width,
                "height": item.output_height,
                "mime_type": item.output_mime_type,
                "download_url": (
                    url_for("generation_api.download", job_id=job.id, item_id=item.id)
                    if item.status == "succeeded" and item.output_path
                    else None
                ),
            }
            for item in job.items
        ],
    }
