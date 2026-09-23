from __future__ import annotations

from ...errors import ServiceError
from ...image_masks import GenerationMask, InvalidMaskError
from ...image_payloads import visual_image_size
from ...models import Asset
from ...storage import ImageStorage, StorageError, StoredImage
from .contracts import GenerationMaskInput


class GenerationMaskManager:
    """Validate and persist the immutable mask input owned by one job."""

    def __init__(self, storage: ImageStorage):
        self.storage = storage

    def validate(
        self,
        upload: GenerationMaskInput | None,
        references: list[Asset],
    ) -> GenerationMask | None:
        if upload is None:
            return None
        if len(references) != 1:
            raise ServiceError(
                "局部重绘必须且只能使用当前操作的一张原图",
                code="mask_reference_count_invalid",
                status_code=422,
            )
        target = references[0]
        if upload.target_asset_id != target.id:
            raise ServiceError(
                "局部重绘原图已变化，请重新框选区域",
                code="mask_target_conflict",
                status_code=409,
            )
        try:
            reference_content = self.storage.read_bytes(target.storage_path)
        except (FileNotFoundError, OSError, StorageError) as exc:
            raise ServiceError(
                "局部重绘原图文件不可用，请重新上传",
                code="reference_unavailable",
                status_code=409,
            ) from exc
        try:
            return GenerationMask.from_upload(
                upload.content,
                reference_size=visual_image_size(reference_content),
            )
        except (InvalidMaskError, ValueError) as exc:
            raise ServiceError(
                str(exc),
                code="invalid_mask",
                status_code=422,
            ) from exc

    def save(
        self,
        mask: GenerationMask,
        *,
        user_id: int,
        workspace_id: str,
        job_id: str,
    ) -> StoredImage:
        return self.storage.save_generation_mask(
            user_id=user_id,
            workspace_id=workspace_id,
            job_id=job_id,
            content=mask.content,
        )
