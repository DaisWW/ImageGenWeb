from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ...config.chat_models import ChatModelRegistry
from ...extensions import db
from ...integrations.openai_chat import OpenAIChatClient
from ...models import (
    ConversationAttachment,
    ConversationMessage,
    ConversationState,
    Workspace,
)
from ...storage import ImageStorage
from ..runtime_logs import RuntimeLogService
from ..settings import SystemSettingsService
from .context import ConversationContextManager
from .operations import ConversationOperation, ConversationOperationRegistry
from .prompt_workflow import PromptDraftWorkflow
from .replies import ConversationReplyService
from .review_workflow import ImageReviewWorkflow
from .support import ConversationDependencies


@dataclass(frozen=True)
class ConversationPage:
    messages: list[ConversationMessage]
    total: int
    has_more: bool


class ConversationService:
    def __init__(
        self,
        chat_models: ChatModelRegistry,
        storage: ImageStorage,
        settings: SystemSettingsService,
        runtime_logs: RuntimeLogService,
        client: OpenAIChatClient | None = None,
    ):
        context = ConversationContextManager(chat_models, storage)
        self.dependencies = ConversationDependencies(
            chat_models=chat_models,
            storage=storage,
            settings=settings,
            runtime_logs=runtime_logs,
            context=context,
            client=client or OpenAIChatClient(),
        )
        self.operations = ConversationOperationRegistry(settings)
        self.replies = ConversationReplyService(self.dependencies, self.operations)
        self.prompt_drafts = PromptDraftWorkflow(self.dependencies, self.operations)
        self.image_reviews = ImageReviewWorkflow(self.dependencies, self.operations)

    @property
    def client(self) -> OpenAIChatClient:
        return self.dependencies.client

    @client.setter
    def client(self, value: OpenAIChatClient) -> None:
        self.dependencies.client = value

    @property
    def chat_models(self) -> ChatModelRegistry:
        return self.dependencies.chat_models

    @property
    def context(self) -> ConversationContextManager:
        return self.dependencies.context

    def list_messages(self, workspace: Workspace, *, limit: int = 200) -> ConversationPage:
        limit = min(500, max(1, limit))
        total = (
            db.session.scalar(
                select(func.count(ConversationMessage.id)).where(
                    ConversationMessage.workspace_id == workspace.id
                )
            )
            or 0
        )
        newest = list(
            db.session.scalars(
                select(ConversationMessage)
                .options(
                    selectinload(ConversationMessage.attachments).selectinload(
                        ConversationAttachment.asset
                    )
                )
                .where(ConversationMessage.workspace_id == workspace.id)
                .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
                .limit(limit)
            )
        )
        return ConversationPage(
            messages=list(reversed(newest)), total=total, has_more=total > limit
        )

    def send(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        content: str,
        attachment_ids: tuple[str, ...] = (),
        generation_reference_ids: tuple[str, ...] = (),
        generation_mode: str = "",
        clarification_reply_to_id: str = "",
        message_id: str = "",
        operation_id: str = "",
    ) -> tuple[ConversationMessage, ConversationMessage]:
        return self.replies.send(
            workspace,
            model_id=model_id,
            content=content,
            attachment_ids=attachment_ids,
            generation_reference_ids=generation_reference_ids,
            generation_mode=generation_mode,
            clarification_reply_to_id=clarification_reply_to_id,
            message_id=message_id,
            operation_id=operation_id,
        )

    def retry(
        self,
        workspace: Workspace,
        *,
        error_message_id: str,
        model_id: str,
        operation_id: str = "",
    ) -> ConversationMessage:
        return self.replies.retry(
            workspace,
            error_message_id=error_message_id,
            model_id=model_id,
            operation_id=operation_id,
        )

    def create_prompt_draft(
        self,
        workspace: Workspace,
        *,
        model_id: str,
        translate_to_english: bool,
        mode: str = "",
        reference_ids: tuple[str, ...] = (),
        creative_direction_id: str = "auto",
    ) -> ConversationMessage:
        return self.prompt_drafts.create_prompt_draft(
            workspace,
            model_id=model_id,
            translate_to_english=translate_to_english,
            mode=mode,
            reference_ids=reference_ids,
            creative_direction_id=creative_direction_id,
        )

    def validate_generation_draft(
        self,
        workspace: Workspace,
        *,
        draft_id: str,
        prompt: str,
        mode: str,
        reference_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        return self.prompt_drafts.validate_generation_draft(
            workspace,
            draft_id=draft_id,
            prompt=prompt,
            mode=mode,
            reference_ids=reference_ids,
        )

    def review_generation_item(
        self,
        item,
        *,
        model_id: str,
    ) -> dict[str, Any]:
        return self.image_reviews.review_generation_item(item, model_id=model_id)

    def state_dict(self, workspace: Workspace) -> dict[str, Any]:
        state = db.session.get(ConversationState, workspace.id)
        return {
            "compacted": False,
            "estimated_context_tokens": state.estimated_context_tokens if state else 0,
            "max_context_tokens": self.chat_models.context.max_context_tokens,
        }

    def operation_state(self, workspace_id: str) -> dict[str, Any]:
        return self.operations.state(workspace_id)

    def cancel_operation(self, workspace_id: str, operation_id: str) -> bool:
        return self.operations.cancel(workspace_id, operation_id)

    def generation_submission(
        self,
        workspace: Workspace,
        *,
        operation_id: str = "",
    ) -> AbstractContextManager[ConversationOperation]:
        return self.operations.generation_submission(workspace, operation_id=operation_id)

    def workspace_mutation(
        self,
        workspace: Workspace,
        label: str,
    ) -> AbstractContextManager[None]:
        return self.operations.workspace_mutation(workspace, label)

    def _workspace_operation(
        self,
        workspace: Workspace,
        kind: str,
        label: str,
        *,
        enforce_chat_capacity: bool = True,
    ) -> AbstractContextManager[None]:
        return self.operations.workspace_operation(
            workspace,
            kind,
            label,
            enforce_chat_capacity=enforce_chat_capacity,
        )
