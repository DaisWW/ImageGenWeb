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
        max_count=workspace_service.MAX_WORKSPACES,
    )


@web.post("/api/workspaces")
@login_required
def create_workspace():
    workspace = services().workspaces.create(current_user.id, str(json_body().get("name", "")))
    return jsonify(workspace=workspace_dict(workspace)), 201


@web.post("/api/workspaces/<workspace_id>/clear")
@login_required
def clear_workspace(workspace_id: str):
    workspace = owned_workspace(workspace_id)
    services().conversations.ensure_idle(workspace.id)
    workspace = services().workspaces.clear(workspace)
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
    services().conversations.ensure_idle(workspace.id)
    services().workspaces.delete(workspace)
    return jsonify(ok=True)


@web.post("/api/workspaces/<workspace_id>/assets")
@login_required
def upload_assets(workspace_id: str):
    workspace = owned_workspace(workspace_id)
    uploads: list[tuple[str, bytes]] = []
    total = 0
    for uploaded in request.files.getlist("references"):
        content = uploaded.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise ServiceError("单张垫图不能超过 10 MiB", status_code=413)
        total += len(content)
        uploads.append((uploaded.filename or "reference", content))
    if total > 40 * 1024 * 1024:
        raise ServiceError("垫图合计不能超过 40 MiB", status_code=413)
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
    services().conversations.ensure_idle(workspace.id)
    services().workspaces.remove_asset(workspace, asset_id)
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
    if not isinstance(attachment_ids, list):
        raise ServiceError("参考图参数无效")
    conversations = services().conversations
    user_message, assistant_message = conversations.send(
        workspace,
        model_id=str(data.get("model_id", "")),
        content=str(data.get("content", "")),
        attachment_ids=tuple(str(item) for item in attachment_ids),
    )
    return jsonify(
        messages=[
            conversation_message_dict(user_message),
            conversation_message_dict(assistant_message),
        ],
        context=conversations.state_dict(workspace),
        workspace=workspace_dict(workspace),
    ), 201


@web.post("/api/workspaces/<workspace_id>/prompt-drafts")
@login_required
def create_prompt_draft(workspace_id: str):
    workspace = owned_workspace(workspace_id)
    data = json_body()
    conversations = services().conversations
    message = conversations.create_prompt_draft(
        workspace,
        model_id=str(data.get("model_id", "")),
        translate_to_english=json_bool(data.get("translate_to_english", False)),
    )
    return jsonify(
        message=conversation_message_dict(message),
        context=conversations.state_dict(workspace),
    ), 201
