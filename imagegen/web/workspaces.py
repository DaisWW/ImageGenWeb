from __future__ import annotations

from flask import jsonify, request
from flask_login import current_user, login_required

from ..errors import ServiceError
from ..serializers import conversation_message_dict, workspace_dict
from . import web
from .shared import (
    json_body,
    json_bool,
    owned_workspace,
    query_limit,
    services,
    workspace_payload,
)


@web.get("/api/workspaces")
@login_required
def list_workspaces():
    workspace_service = services().workspaces
    workspaces = workspace_service.list(current_user.id)
    return jsonify(
        workspaces=[workspace_payload(workspace) for workspace in workspaces],
        max_count=workspace_service.max_workspaces,
    )


@web.post("/api/workspaces")
@login_required
def create_workspace():
    data = json_body()
    workspace = services().workspaces.create(
        current_user.id,
        str(data.get("name", "")),
        str(data.get("kind", "image")),
    )
    return jsonify(workspace=workspace_dict(workspace)), 201


@web.put("/api/workspaces/order")
@login_required
def reorder_workspaces():
    workspace_ids = json_body().get("workspace_ids", [])
    if not isinstance(workspace_ids, list):
        raise ServiceError("工作站排序数据无效")
    services().workspaces.reorder(
        current_user.id,
        [str(workspace_id) for workspace_id in workspace_ids],
    )
    return jsonify(ok=True)


@web.post("/api/workspaces/<workspace_id>/clear")
@login_required
def clear_workspace(workspace_id: str):
    workspace = owned_workspace(workspace_id)
    application_services = services()
    with application_services.conversations.workspace_mutation(workspace, "正在清空工作站"):
        workspace = application_services.workspaces.clear(workspace)
    return jsonify(workspace=workspace_dict(workspace))


@web.patch("/api/workspaces/<workspace_id>")
@login_required
def update_workspace(workspace_id: str):
    workspace = services().workspaces.update(owned_workspace(workspace_id), json_body())
    return jsonify(workspace=workspace_dict(workspace))


@web.delete("/api/workspaces/<workspace_id>")
@login_required
def delete_workspace(workspace_id: str):
    workspace = owned_workspace(workspace_id)
    application_services = services()
    with application_services.conversations.workspace_mutation(workspace, "正在删除工作站"):
        application_services.workspaces.delete(workspace)
    return jsonify(ok=True)


@web.post("/api/workspaces/<workspace_id>/assets")
@login_required
def upload_assets(workspace_id: str):
    workspace = owned_workspace(workspace_id)
    runtime = services().settings.runtime()
    uploads: list[tuple[str, bytes]] = []
    total = 0
    files = request.files.getlist("references")
    if len(files) > runtime.max_assets_per_workspace:
        raise ServiceError(
            f"单次最多上传 {runtime.max_assets_per_workspace} 张参考图", status_code=413
        )
    for uploaded in files:
        content = uploaded.read(runtime.max_attachment_bytes + 1)
        if len(content) > runtime.max_attachment_bytes:
            raise ServiceError(f"单张垫图不能超过 {runtime.max_attachment_mb} MiB", status_code=413)
        total += len(content)
        uploads.append((uploaded.filename or "reference", content))
    if total > runtime.max_attachment_total_bytes:
        raise ServiceError(
            f"垫图合计不能超过 {runtime.max_attachment_total_mb} MiB", status_code=413
        )
    assets = services().workspaces.add_assets(workspace, uploads)
    return jsonify(
        assets=[workspace_dict(workspace, [asset])["assets"][0] for asset in assets]
    ), 201


@web.put("/api/workspaces/<workspace_id>/assets/order")
@login_required
def reorder_assets(workspace_id: str):
    workspace = owned_workspace(workspace_id)
    asset_ids = json_body().get("asset_ids", [])
    if not isinstance(asset_ids, list):
        raise ServiceError("垫图排序数据无效")
    services().workspaces.reorder_assets(workspace, [str(item) for item in asset_ids])
    return jsonify(ok=True)


@web.delete("/api/workspaces/<workspace_id>/assets/<asset_id>")
@login_required
def remove_asset(workspace_id: str, asset_id: str):
    workspace = owned_workspace(workspace_id)
    application_services = services()
    with application_services.conversations.workspace_mutation(workspace, "正在删除垫图"):
        application_services.workspaces.remove_asset(workspace, asset_id)
    return jsonify(ok=True)


@web.get("/api/workspaces/<workspace_id>/messages")
@login_required
def list_conversation_messages(workspace_id: str):
    workspace = owned_workspace(workspace_id)
    conversations = services().conversations
    page = conversations.list_messages(workspace, limit=query_limit())
    return jsonify(
        messages=[conversation_message_dict(message) for message in page.messages],
        total=page.total,
        has_more=page.has_more,
        context=conversations.state_dict(workspace),
        conversation_operation=conversations.operation_state(workspace.id),
    )


@web.post("/api/workspaces/<workspace_id>/messages")
@login_required
def send_conversation_message(workspace_id: str):
    workspace = owned_workspace(workspace_id)
    data = json_body()
    attachment_ids = data.get("attachment_ids", [])
    generation_reference_ids = data.get("generation_reference_ids", [])
    if not isinstance(attachment_ids, list) or not isinstance(generation_reference_ids, list):
        raise ServiceError("参考图参数无效")
    conversations = services().conversations
    user_message, assistant_message = conversations.send(
        workspace,
        model_id=str(data.get("model_id", "")),
        content=str(data.get("content", "")),
        attachment_ids=tuple(str(item) for item in attachment_ids),
        generation_reference_ids=tuple(str(item) for item in generation_reference_ids),
        generation_mode=str(data.get("generation_mode", "")),
        clarification_reply_to_id=str(data.get("clarification_reply_to_id", "")),
        message_id=str(data.get("message_id", "")),
        operation_id=str(data.get("operation_id", "")),
    )
    return jsonify(
        messages=[
            conversation_message_dict(user_message),
            conversation_message_dict(assistant_message),
        ],
        context=conversations.state_dict(workspace),
        workspace=workspace_dict(workspace),
    ), 201


@web.post("/api/workspaces/<workspace_id>/messages/<message_id>/retry")
@login_required
def retry_conversation_message(workspace_id: str, message_id: str):
    workspace = owned_workspace(workspace_id)
    data = json_body()
    conversations = services().conversations
    message = conversations.retry(
        workspace,
        error_message_id=message_id,
        model_id=str(data.get("model_id", "")),
        operation_id=str(data.get("operation_id", "")),
    )
    return jsonify(
        message=conversation_message_dict(message),
        context=conversations.state_dict(workspace),
    ), 201


@web.post("/api/workspaces/<workspace_id>/operations/<operation_id>/cancel")
@login_required
def cancel_workspace_operation(workspace_id: str, operation_id: str):
    workspace = owned_workspace(workspace_id)
    canceled = services().conversations.cancel_operation(workspace.id, operation_id)
    return jsonify(canceled=True, operation_id=operation_id, active=canceled)


@web.post("/api/workspaces/<workspace_id>/prompt-drafts")
@login_required
def create_prompt_draft(workspace_id: str):
    workspace = owned_workspace(workspace_id)
    data = json_body()
    reference_ids = data.get("reference_ids", [])
    if not isinstance(reference_ids, list):
        raise ServiceError("垫图参数无效")
    conversations = services().conversations
    message = conversations.create_prompt_draft(
        workspace,
        model_id=str(data.get("model_id", "")),
        translate_to_english=json_bool(data.get("translate_to_english", False)),
        mode=str(data.get("mode", "")),
        reference_ids=tuple(str(item) for item in reference_ids),
        creative_direction_id=str(data.get("creative_direction_id", "auto")),
    )
    return jsonify(
        message=conversation_message_dict(message),
        context=conversations.state_dict(workspace),
    ), 201
