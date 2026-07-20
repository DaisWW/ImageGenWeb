from __future__ import annotations

import hashlib
import io
import os
import shutil
import uuid
import warnings
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

IMAGE_FORMATS = {
    "png": ("png", "image/png"),
    "jpeg": ("jpg", "image/jpeg"),
    "webp": ("webp", "image/webp"),
}
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 40_000_000


class StorageError(RuntimeError):
    pass


class InvalidImageError(StorageError):
    pass


@dataclass(frozen=True)
class StoredImage:
    relative_path: str
    mime_type: str
    extension: str
    byte_count: int
    width: int
    height: int
    sha256: str


@dataclass(frozen=True)
class StoredOutput:
    image: StoredImage
    thumbnail_path: str


class ImageStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def inspect(self, content: bytes) -> StoredImage:
        return self._inspect(content, static=False)

    def inspect_static(self, content: bytes) -> StoredImage:
        return self._inspect(content, static=True)

    @staticmethod
    def _inspect(content: bytes, *, static: bool) -> StoredImage:
        if not content:
            raise InvalidImageError("图片内容为空")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    width, height = image.size
                    image_format = (image.format or "").lower()
                    if image_format not in IMAGE_FORMATS:
                        raise InvalidImageError("仅支持 PNG、JPEG 和 WebP 图片")
                    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                        raise InvalidImageError(f"图片单边不能超过 {MAX_IMAGE_DIMENSION} 像素")
                    if width * height > MAX_IMAGE_PIXELS:
                        raise InvalidImageError(f"图片总像素不能超过 {MAX_IMAGE_PIXELS:,}")
                    if static and getattr(image, "n_frames", 1) > 1:
                        raise InvalidImageError("图库仅支持静态图片")
                    image.load()
        except InvalidImageError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise InvalidImageError("图片像素数量超过安全限制") from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise InvalidImageError("文件不是有效图片") from exc
        extension, mime_type = IMAGE_FORMATS[image_format]
        return StoredImage(
            relative_path="",
            mime_type=mime_type,
            extension=extension,
            byte_count=len(content),
            width=width,
            height=height,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def save_reference(
        self,
        *,
        user_id: int,
        workspace_id: str,
        asset_id: str,
        content: bytes,
    ) -> StoredImage:
        inspected = self.inspect(content)
        relative = Path("users") / str(user_id) / "workspaces" / workspace_id / "references"
        relative /= f"{asset_id}.{inspected.extension}"
        self._atomic_write(relative, content)
        return replace(inspected, relative_path=relative.as_posix())

    def save_library_image(
        self,
        *,
        user_id: int,
        image_id: str,
        content: bytes,
        inspected: StoredImage,
    ) -> StoredOutput:
        relative = Path("users") / str(user_id) / "library"
        relative /= f"{image_id}.{inspected.extension}"
        return self._save_with_thumbnail(relative, content, inspected)

    def save_output(
        self,
        *,
        user_id: int,
        workspace_id: str,
        job_id: str,
        item_id: str,
        content: bytes,
    ) -> StoredOutput:
        inspected = self.inspect(content)
        directory = Path("users") / str(user_id) / "workspaces" / workspace_id / "generations"
        directory /= job_id
        relative = directory / f"{item_id}.{inspected.extension}"
        return self._save_with_thumbnail(relative, content, inspected)

    def _save_with_thumbnail(
        self,
        relative: Path,
        content: bytes,
        inspected: StoredImage,
    ) -> StoredOutput:
        thumbnail = relative.with_suffix(".thumb.webp")
        self._atomic_write(relative, content)
        try:
            self._atomic_write(thumbnail, self._thumbnail(content))
        except Exception:
            self.delete(relative.as_posix())
            raise
        return StoredOutput(
            image=replace(inspected, relative_path=relative.as_posix()),
            thumbnail_path=thumbnail.as_posix(),
        )

    def read(self, relative_path: str) -> Path:
        path = self._resolve(relative_path)
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        return path

    def read_bytes(self, relative_path: str) -> bytes:
        return self.read(relative_path).read_bytes()

    def healthcheck(self) -> None:
        relative = Path(f".healthcheck-{uuid.uuid4().hex}.tmp")
        self._atomic_write(relative, b"ok")
        self.delete(relative.as_posix())

    def delete(self, relative_path: str | None) -> None:
        if not relative_path:
            return
        try:
            self._resolve(relative_path).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"删除图片失败：{exc}") from exc

    def delete_job_directory(self, user_id: int, workspace_id: str, job_id: str) -> None:
        relative = (
            Path("users") / str(user_id) / "workspaces" / workspace_id / "generations" / job_id
        )
        self._delete_tree(relative)

    def delete_workspace(self, user_id: int, workspace_id: str) -> None:
        relative = Path("users") / str(user_id) / "workspaces" / workspace_id
        self._delete_tree(relative)

    def _delete_tree(self, relative: Path) -> None:
        path = self._resolve(relative.as_posix())
        try:
            if path.exists():
                shutil.rmtree(path)
        except OSError as exc:
            raise StorageError(f"删除存储目录失败：{exc}") from exc

    def _resolve(self, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute():
            raise StorageError("存储路径无效")
        resolved = (self.root / relative).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise StorageError("存储路径越界") from exc
        return resolved

    def _atomic_write(self, relative: Path, content: bytes) -> None:
        target = self._resolve(relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise StorageError(f"保存图片失败：{exc}") from exc

    @staticmethod
    def _thumbnail(content: bytes) -> bytes:
        with Image.open(io.BytesIO(content)) as source:
            thumbnail = ImageOps.exif_transpose(source)
            thumbnail.thumbnail((640, 640), Image.Resampling.LANCZOS)
            if thumbnail.mode not in {"RGB", "RGBA"}:
                thumbnail = thumbnail.convert("RGBA")
            output = io.BytesIO()
            thumbnail.save(output, format="WEBP", quality=82, method=4)
            return output.getvalue()
