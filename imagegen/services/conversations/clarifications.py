from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select

from ...errors import ServiceError
from ...extensions import db
from ...models import Asset, ConversationMessage, Workspace


class ClarificationReferenceResolver:
    """Resolve pad images only for an explicitly selected open clarification."""

    def __init__(self, load_assets: Callable[[Workspace, tuple[str, ...]], list[Asset]]):
        self.load_assets = load_assets

    def resolve(
        self,
        workspace: Workspace,
        clarification_message_id: str,
    ) -> tuple[list[Asset], str] | None:
        clarification = self.ensure_open(workspace, clarification_message_id)
        reference_ids = tuple(
            str(item)
            for item in (clarification.payload or {}).get("reference_ids", [])
            if str(item).strip()
        )
        if not reference_ids:
            return None
        try:
            references = self.load_assets(workspace, reference_ids)
        except ServiceError:
            return None
        if not references:
            return None
        mode = str((clarification.payload or {}).get("generation_mode", "auto")).strip().lower()
        return references, mode if mode in {"auto", "img2img"} else "auto"

    @staticmethod
    def ensure_open(
        workspace: Workspace,
        clarification_message_id: str,
    ) -> ConversationMessage:
        messages = db.session.scalars(
            select(ConversationMessage)
            .where(
                ConversationMessage.workspace_id == workspace.id,
                ConversationMessage.role == "assistant",
            )
            .order_by(
                ConversationMessage.created_at.desc(),
                ConversationMessage.id.desc(),
            )
            .limit(40)
        )
        for message in messages:
            payload = message.payload or {}
            status = str(payload.get("status", "")).strip().lower()
            if status == "ready" and message.kind == "prompt_draft":
                break
            if status == "needs_clarification":
                if message.id == clarification_message_id:
                    return message
                break
        raise ServiceError(
            "澄清问题已结束，请选择最新问题继续回答",
            code="conversation_clarification_closed",
            status_code=409,
        )
