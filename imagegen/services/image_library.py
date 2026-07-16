from __future__ import annotations

from contextlib import nullcontext
from threading import Lock
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..errors import ServiceError
from ..extensions import db
from ..models import LibraryImage, User, new_public_id
from ..storage import ImageStorage, StoredImage

LIBRARY_PAGE_SIZE = 60
MAX_LIBRARY_IMAGES = 200
MAX_LIBRARY_BYTES = 2 * 1024 * 1024 * 1024


class ImageLibraryService:
    def __init__(self, storage: ImageStorage):
        self.storage = storage
        self._sqlite_write_lock = Lock()

    def page(
        self,
        user_id: int,
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[LibraryImage], int]:
        total = (
            db.session.scalar(
                select(func.count(LibraryImage.id)).where(LibraryImage.user_id == user_id)
            )
            or 0
        )
        images = list(
            db.session.scalars(
                select(LibraryImage)
                .where(LibraryImage.user_id == user_id)
                .order_by(LibraryImage.created_at.desc(), LibraryImage.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        return images, total

    def add(
        self,
        user_id: int,
        uploads: Iterable[tuple[str, bytes]],
    ) -> tuple[list[LibraryImage], int]:
        uploads = list(uploads)
        if not uploads:
            raise ServiceError("请选择图片")

        prepared = [
            (original_name, content, self.storage.inspect_static(content))
            for original_name, content in uploads
        ]
        write_lock = (
            self._sqlite_write_lock if db.engine.dialect.name == "sqlite" else nullcontext()
        )
        with write_lock:
            for attempt in range(2):
                try:
                    return self._add_once(user_id, prepared)
                except IntegrityError:
                    if attempt:
                        raise
        raise AssertionError("unreachable")

    def _add_once(
        self,
        user_id: int,
        uploads: list[tuple[str, bytes, StoredImage]],
    ) -> tuple[list[LibraryImage], int]:
        saved_paths: list[str] = []
        try:
            db.session.scalar(select(User.id).where(User.id == user_id).with_for_update())
            hashes = {inspected.sha256 for _name, _content, inspected in uploads}
            existing_by_hash = {
                image.sha256: image
                for image in db.session.scalars(
                    select(LibraryImage).where(
                        LibraryImage.user_id == user_id,
                        LibraryImage.sha256.in_(hashes),
                    )
                )
            }
            pending_by_hash: dict[str, tuple[str, bytes, StoredImage]] = {}
            for original_name, content, inspected in uploads:
                if inspected.sha256 not in existing_by_hash:
                    pending_by_hash.setdefault(
                        inspected.sha256, (original_name, content, inspected)
                    )

            current_count, current_bytes = db.session.execute(
                select(
                    func.count(LibraryImage.id),
                    func.coalesce(func.sum(LibraryImage.byte_count), 0),
                ).where(LibraryImage.user_id == user_id)
            ).one()
            if current_count + len(pending_by_hash) > MAX_LIBRARY_IMAGES:
                raise ServiceError(
                    f"每个账户最多保存 {MAX_LIBRARY_IMAGES} 张图库图片",
                    code="library_quota",
                    status_code=409,
                )
            pending_bytes = sum(item[2].byte_count for item in pending_by_hash.values())
            if current_bytes + pending_bytes > MAX_LIBRARY_BYTES:
                raise ServiceError(
                    "图库原图累计不能超过 2 GiB",
                    code="library_quota",
                    status_code=409,
                )

            created_by_hash: dict[str, LibraryImage] = {}
            for sha256, (original_name, content, inspected) in pending_by_hash.items():
                image_id = new_public_id()
                stored = self.storage.save_library_image(
                    user_id=user_id,
                    image_id=image_id,
                    content=content,
                    inspected=inspected,
                )
                saved_paths.extend([stored.image.relative_path, stored.thumbnail_path])
                image = LibraryImage(
                    id=image_id,
                    user_id=user_id,
                    original_name=(original_name.strip() or f"image.{stored.image.extension}")[
                        :255
                    ],
                    storage_path=stored.image.relative_path,
                    thumbnail_path=stored.thumbnail_path,
                    mime_type=stored.image.mime_type,
                    byte_count=stored.image.byte_count,
                    width=stored.image.width,
                    height=stored.image.height,
                    sha256=sha256,
                )
                created_by_hash[sha256] = image
                db.session.add(image)

            results = [
                existing_by_hash.get(inspected.sha256) or created_by_hash[inspected.sha256]
                for _name, _content, inspected in uploads
            ]
            db.session.commit()
            return results, len(created_by_hash)
        except Exception:
            db.session.rollback()
            for path in saved_paths:
                self.storage.delete(path)
            raise

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
        self.storage.delete(image.thumbnail_path)
        db.session.delete(image)
        db.session.commit()
