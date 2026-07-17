from __future__ import annotations

import base64
from typing import Any

from ...errors import ServiceError
from ...extensions import db
from ...integrations.openai_chat import OpenAIChatError
from ...models import GenerationItem, Workspace, utcnow
from ..image_reviews import ImageReviewEvaluation
from .operations import ConversationOperationRegistry
from .support import ConversationDependencies, ConversationSupport


class ImageReviewWorkflow(ConversationSupport):
    def __init__(
        self,
        dependencies: ConversationDependencies,
        operations: ConversationOperationRegistry,
    ):
        super().__init__(dependencies)
        self.operations = operations

    def review_generation_item(
        self,
        item: GenerationItem,
        *,
        model_id: str,
    ) -> dict[str, Any]:
        if item.status != "succeeded" or not item.output_path or not item.output_mime_type:
            raise ServiceError("只有生成成功的图片可以进行 AI 验收")
        workspace = db.session.get(Workspace, item.job.workspace_id)
        if workspace is None:
            raise ServiceError("工作站不存在", status_code=404)
        with self.operations.workspace_operation(workspace, "image_review", "正在进行 AI 图片验收"):
            return self._review_generation_item(workspace, item, model_id=model_id)

    def _review_generation_item(
        self,
        workspace: Workspace,
        item: GenerationItem,
        *,
        model_id: str,
    ) -> dict[str, Any]:
        model = self._model(model_id)
        runtime = self.settings.runtime()
        references = list(item.job.references)[: max(0, runtime.max_chat_attachments - 1)]
        media = [
            (
                self.storage.read_bytes(item.output_path),
                item.output_mime_type,
                "待验收结果（图像 1）",
            ),
            *(
                (
                    self.storage.read_bytes(reference.asset.storage_path),
                    reference.asset.mime_type,
                    f"生成参考图 {reference.position + 1}",
                )
                for reference in references
            ),
        ]
        if (
            sum(len(content) for content, _mime_type, _label in media)
            > runtime.max_attachment_total_bytes
        ):
            raise ServiceError(f"验收图片合计不能超过 {runtime.max_attachment_total_mb} MiB")
        parts: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"请验收这次生成结果。\n生成提示词：\n{item.job.prompt}\n\n"
                    "图像按下面标注的顺序提供。"
                ),
            }
        ]
        for content, mime_type, label in media:
            parts.append({"type": "text", "text": label})
            encoded = base64.b64encode(content).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                }
            )
        raw_checks = item.job.workflow.get("hard_checks", [])
        expected_checks = (
            tuple(str(check) for check in raw_checks) if isinstance(raw_checks, list) else ()
        )
        evaluation = ImageReviewEvaluation(
            generation_mode=item.job.mode,
            reference_count=len(references),
            expected_checks=expected_checks,
        )
        try:
            result = self.client.complete(
                model,
                system=evaluation.system_prompt(),
                messages=[{"role": "user", "content": parts}],
                max_output_tokens=min(model.max_output_tokens, 1800),
            )
        except OpenAIChatError as exc:
            self._raise_chat_error(workspace, model, "chat.image_review", exc)
        review = evaluation.parse(result.content)
        review.update(
            {
                "reviewed_at": utcnow().isoformat(),
                "provider_id": model.identifier,
                "provider_label": model.label,
                "model": model.model,
                "upstream_request_id": result.request_id,
                "elapsed_seconds": result.elapsed_seconds,
            }
        )
        item.review = review
        self._record_chat_success(
            workspace,
            model,
            "chat.image_review",
            result,
            details={
                "job_id": item.job_id,
                "item_id": item.id,
                "outcome": review["verdict"],
                "reference_count": len(references),
            },
        )
        db.session.commit()
        return review
