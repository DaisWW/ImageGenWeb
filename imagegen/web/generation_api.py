from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from flask import (
    Blueprint,
    current_app,
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
from ..models import GenerationJob
from ..serializers import job_status_dict
from ..services import SubmitGeneration
from . import web
from .shared import channels, image_extension, json_body, services, storage

api = Blueprint("generation_api", __name__, url_prefix="/api/v1")
SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "snow-image-api"
# Allow a 12,000-character prompt encoded as JSON Unicode escapes plus metadata.
MAX_GENERATION_REQUEST_BYTES = 192 * 1024


@web.get("/account/generation-api")
@login_required
def generation_api_page():
    application_services = services()
    key = application_services.generation_api_keys.get_key(current_user.id)
    return render_template(
        "pages/generation-api.html",
        key_enabled=key is not None,
        key_workspace_id=key.workspace_id if key else None,
        workspaces=application_services.workspaces.list(current_user.id),
        base_url=request.url_root.rstrip("/"),
    )


@web.route("/account/generation-api/key", methods=["POST", "DELETE"])
def generation_api_key():
    if not current_user.is_authenticated:
        raise ServiceError("请先登录", code="login_required", status_code=401)
    keys = services().generation_api_keys
    if request.method == "DELETE":
        keys.revoke_key(current_user.id)
        return "", 204

    workspace_id = _uuid_hex(json_body().get("workspace_id"), "请选择工作站")
    token = keys.issue_key(current_user.id, workspace_id)
    response = jsonify(api_key=token)
    response.headers["Cache-Control"] = "no-store"
    return response


@web.get("/account/generation-api/skill")
@login_required
def generation_api_skill():
    bundle = BytesIO()
    with ZipFile(bundle, "w", compression=ZIP_DEFLATED) as archive:
        for entry in (
            "SKILL.md",
            "references/api.md",
            "references/request.json",
            "scripts/generate.py",
        ):
            archive.write(SKILL_DIR / entry, f"{SKILL_DIR.name}/{entry}")
    bundle.seek(0)
    return send_file(
        bundle,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{SKILL_DIR.name}.zip",
    )


@api.before_request
def authenticate_generation_api():
    authorization = request.authorization
    token = authorization.token if authorization and authorization.type == "bearer" else None
    key = services().generation_api_keys.authenticate(token or "")
    g.generation_api_key = key
    g.generation_api_user_id = key.user_id


@api.after_request
def generation_api_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    if response.status_code == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="Snow AI Studio"'
    return response


@api.get("/config")
def config():
    key = g.generation_api_key
    runtime = services().settings.runtime()
    registry = channels()
    return jsonify(
        workspace={"id": key.workspace_id, "name": key.workspace.name},
        available_balance_rmb=format(key.user.available_balance_rmb, ".4f"),
        channels=[
            channel.public_dict()
            for channel in registry.list(include_disabled=False)
            if channel.configured and "text2img" in channel.capabilities.modes
        ],
        limits={
            "max_batch_images": runtime.max_batch_images,
            "max_prompt_characters": runtime.max_prompt_characters,
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
    generation = _parse_generation(_submission_body())
    job, created = services().generation_api.submit(g.generation_api_key, request_id, generation)
    response = make_response(jsonify(job=_job_payload(job)), 202 if created else 200)
    response.headers["Location"] = url_for("generation_api.status", job_id=job.id)
    return response


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
    if not isinstance(value, str):
        raise ServiceError(message, code=code)
    try:
        return UUID(value).hex
    except ValueError as exc:
        raise ServiceError(message, code=code) from exc


def _parse_generation(data: dict) -> SubmitGeneration:
    defaults = {
        "channel_id": "",
        "model": "",
        "prompt": "",
        "size": "auto",
        "quality": "auto",
        "output_format": "png",
    }
    if data.keys() - (defaults.keys() | {"batch_count", "compression"}):
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
    for name in ("quality", "output_format"):
        values[name] = values[name].lower()
    batch_count = data.get("batch_count", 1)
    compression = data.get("compression", 90)
    if type(batch_count) is not int or type(compression) is not int:
        raise ServiceError("生成数量和压缩质量必须是整数")

    return SubmitGeneration(
        **values,
        mode="text2img",
        batch_count=batch_count,
        compression=compression,
        reference_ids=(),
    )


def _submission_body() -> dict:
    if not request.is_json:
        raise ServiceError("请求内容必须是 JSON")
    content = request.stream.read(MAX_GENERATION_REQUEST_BYTES + 1)
    if len(content) > MAX_GENERATION_REQUEST_BYTES:
        raise ServiceError("生图请求超过 192 KiB 限制", code="payload_too_large", status_code=413)
    try:
        data = current_app.json.loads(content)
    except (ValueError, RecursionError) as exc:
        raise ServiceError("请求内容必须是 JSON") from exc
    if not isinstance(data, dict):
        raise ServiceError("请求内容必须是 JSON")
    return data


def _job_payload(job: GenerationJob) -> dict:
    status = job_status_dict(job, channels())
    return {
        "id": job.id,
        "request_id": job.api_request_id,
        "workspace_id": job.workspace_id,
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
