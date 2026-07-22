from __future__ import annotations

from sqlalchemy import select

from ...config.channels import Channel
from ...errors import ServiceError
from ...extensions import db
from ...models import Asset, Workspace
from ..common import normalize_image_size
from ..settings import SystemSettingsService
from .contracts import GENERATION_QUALITIES, SubmitGeneration


class GenerationRequestValidator:
    def __init__(self, settings: SystemSettingsService):
        self.settings = settings

    def load_references(self, workspace: Workspace, reference_ids: tuple[str, ...]) -> list[Asset]:
        if len(reference_ids) != len(set(reference_ids)):
            raise ServiceError("垫图不能重复")
        if not reference_ids:
            return []
        assets = list(
            db.session.scalars(
                select(Asset).where(
                    Asset.workspace_id == workspace.id,
                    Asset.id.in_(reference_ids),
                    Asset.deleted_at.is_(None),
                )
            )
        )
        by_id = {asset.id: asset for asset in assets}
        if any(asset_id not in by_id for asset_id in reference_ids):
            raise ServiceError("选择的垫图不存在")
        return [by_id[asset_id] for asset_id in reference_ids]

    def validate_request(
        self, channel: Channel, request: SubmitGeneration, workspace_kind: str
    ) -> str:
        runtime = self.settings.runtime()
        if workspace_kind != "image":
            raise ServiceError("工作站类型无效")
        if request.mode not in channel.capabilities.modes:
            raise ServiceError(f"{channel.label} 不支持当前生成模式")
        prompt = request.prompt.strip()
        if not prompt or len(prompt) > runtime.max_prompt_characters:
            raise ServiceError(f"提示词长度必须在 1 到 {runtime.max_prompt_characters} 个字符之间")
        normalized_size = normalize_image_size(request.size)
        if request.output_format not in channel.capabilities.formats:
            raise ServiceError(f"{channel.label} 不支持格式 {request.output_format}")
        if request.quality not in GENERATION_QUALITIES:
            raise ServiceError("生成质量无效")
        if request.transparent_background and request.output_format not in {"png", "webp"}:
            raise ServiceError("透明背景仅支持 PNG 或 WebP 格式")
        if not 0 <= request.compression <= 100:
            raise ServiceError("压缩质量必须在 0 到 100 之间")
        if not 1 <= request.batch_count <= runtime.max_batch_images:
            raise ServiceError(f"单批生成张数必须在 1 到 {runtime.max_batch_images} 之间")
        if request.item_prompts:
            if len(request.item_prompts) != request.batch_count:
                raise ServiceError("逐图提示词数量必须与生成张数一致")
            if any(
                not str(item).strip() or len(str(item).strip()) > runtime.max_prompt_characters
                for item in request.item_prompts
            ):
                raise ServiceError("逐图提示词长度无效")
        return normalized_size

    def validate_references(self, channel: Channel, mode: str, references: list[Asset]) -> None:
        if mode == "img2img" and not references:
            raise ServiceError("垫图生图至少需要一张垫图")
        if mode == "text2img" and references:
            raise ServiceError("文生图任务不能携带垫图")
        runtime = self.settings.runtime()
        if any(asset.byte_count > runtime.max_attachment_bytes for asset in references):
            raise ServiceError(f"单张参考图不能超过 {runtime.max_attachment_mb} MiB")
        if sum(asset.byte_count for asset in references) > runtime.max_attachment_total_bytes:
            raise ServiceError(f"参考图合计不能超过 {runtime.max_attachment_total_mb} MiB")
        capabilities = channel.capabilities
        if len(references) > capabilities.max_reference_images:
            raise ServiceError(
                f"{channel.label} 最多支持 {capabilities.max_reference_images} 张垫图"
            )
        if any(
            asset.byte_count > capabilities.max_reference_image_mb * 1024 * 1024
            for asset in references
        ):
            raise ServiceError(
                f"{channel.label} 的单张垫图不能超过 {capabilities.max_reference_image_mb} MiB"
            )
        if (
            sum(asset.byte_count for asset in references)
            > capabilities.max_reference_total_mb * 1024 * 1024
        ):
            raise ServiceError(
                f"{channel.label} 的垫图合计不能超过 {capabilities.max_reference_total_mb} MiB"
            )
