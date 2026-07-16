from __future__ import annotations

from typing import Iterable

from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import LibraryImage, new_public_id
from ..storage import ImageStorage


class ImageLibraryService:
    def __init__(self, storage: ImageStorage):
        self.storage = storage

    def list(self, user_id: int) -> list[LibraryImage]:
        return list(
            db.session.scalars(
                select(LibraryImage)
                .where(LibraryImage.user_id == user_id)
                .order_by(LibraryImage.created_at.desc())
            )
        )

    def add(
        self,
        user_id: int,
        uploads: Iterable[tuple[str, bytes]],
    ) -> tuple[list[LibraryImage], int]:
        uploads = list(uploads)
        if not uploads:
            raise ServiceError("请选择图片")

        saved_paths: list[str] = []
        results: list[LibraryImage] = []
        created_by_hash: dict[str, LibraryImage] = {}
        try:
            for original_name, content in uploads:
                inspected = self.storage.inspect_static(content)
                image = created_by_hash.get(inspected.sha256) or db.session.scalar(
                    select(LibraryImage).where(
                        LibraryImage.user_id == user_id,
                        LibraryImage.sha256 == inspected.sha256,
                    )
                )
                if image is None:
                    image_id = new_public_id()
                    stored = self.storage.save_library_image(
                        user_id=user_id,
                        image_id=image_id,
                        content=content,
                    )
                    image = LibraryImage(
                        id=image_id,
                        user_id=user_id,
                        original_name=(original_name or f"image.{stored.extension}")[:255],
                        storage_path=stored.relative_path,
                        mime_type=stored.mime_type,
                        byte_count=stored.byte_count,
                        width=stored.width,
                        height=stored.height,
                        sha256=stored.sha256,
                    )
                    saved_paths.append(stored.relative_path)
                    created_by_hash[stored.sha256] = image
                    db.session.add(image)
                results.append(image)
            db.session.commit()
        except Exception:
            db.session.rollback()
            for path in saved_paths:
                self.storage.delete(path)
            raise
        return results, len(created_by_hash)

    def get(self, user_id: int, image_id: str) -> LibraryImage:
        image = db.session.scalar(
            select(LibraryImage).where(
                LibraryImage.id == image_id,
                LibraryImage.user_id == user_id,
            )
        )
        if image is None:
            raise ServiceError("图库图片不存在", status_code=404)
        return image

    def delete(self, user_id: int, image_id: str) -> None:
        image = self.get(user_id, image_id)
        self.storage.delete(image.storage_path)
        db.session.delete(image)
        db.session.commit()
