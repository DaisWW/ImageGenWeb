from __future__ import annotations

import hashlib
import io
import os
import shutil
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

IMAGE_FORMATS = {
    "png": ("png", "image/png"),
    "jpeg": ("jpg", "image/jpeg"),
    "webp": ("webp", "image/webp"),
}
ANIMATION_FORMATS = {
    "webp": ("webp", "image/webp"),
    "gif": ("gif", "image/gif"),
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


@dataclass(frozen=True)
class StoredAnimation:
    relative_path: str
    mime_type: str
    extension: str
    byte_count: int


class ImageStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def inspect(self, content: bytes) -> StoredImage:
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
        return StoredImage(
            relative_path=relative.as_posix(),
            mime_type=inspected.mime_type,
            extension=inspected.extension,
            byte_count=inspected.byte_count,
            width=inspected.width,
            height=inspected.height,
            sha256=inspected.sha256,
        )

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
        thumbnail = directory / f"{item_id}.thumb.webp"
        self._atomic_write(relative, content)
        try:
            thumbnail_bytes = self._thumbnail(content)
            self._atomic_write(thumbnail, thumbnail_bytes)
        except Exception:
            self.delete(relative.as_posix())
            raise
        return StoredOutput(
            image=StoredImage(
                relative_path=relative.as_posix(),
                mime_type=inspected.mime_type,
                extension=inspected.extension,
                byte_count=inspected.byte_count,
                width=inspected.width,
                height=inspected.height,
                sha256=inspected.sha256,
            ),
            thumbnail_path=thumbnail.as_posix(),
        )

    def save_animation(
        self,
        *,
        user_id: int,
        workspace_id: str,
        job_id: str,
        frame_paths: list[str],
        output_format: str,
        fps: int,
        loop: bool,
    ) -> StoredAnimation:
        if output_format not in ANIMATION_FORMATS:
            raise StorageError("动画导出格式无效")
        if not frame_paths:
            raise StorageError("动画没有可用帧")
        extension, mime_type = ANIMATION_FORMATS[output_format]
        directory = Path("users") / str(user_id) / "workspaces" / workspace_id
        relative = directory / "generations" / job_id / f"animation.{extension}"
        target = self._resolve(relative.as_posix())
        if target.is_file():
            return StoredAnimation(
                relative_path=relative.as_posix(),
                mime_type=mime_type,
                extension=extension,
                byte_count=target.stat().st_size,
            )

        frames: list[Image.Image] = []
        expected_size: tuple[int, int] | None = None
        for frame_path in frame_paths:
            with Image.open(self.read(frame_path)) as source:
                source.seek(0)
                frame = ImageOps.exif_transpose(source)
                frame.load()
                frame = frame.convert("RGBA" if "A" in frame.getbands() else "RGB")
                if expected_size is None:
                    expected_size = frame.size
                elif frame.size != expected_size:
                    raise StorageError("动画帧尺寸不一致")
                frames.append(frame.copy())

        duration = max(1, round(1000 / max(1, fps)))
        output = io.BytesIO()
        options = {
            "save_all": True,
            "append_images": frames[1:],
            "duration": duration,
        }
        if output_format == "gif":
            options["disposal"] = 2
            if loop:
                options["loop"] = 0
            frames[0].save(output, format="GIF", **options)
        else:
            options.update(lossless=True, quality=90, method=4, loop=0 if loop else 1)
            frames[0].save(output, format="WEBP", **options)
        content = output.getvalue()
        self._atomic_write(relative, content)
        return StoredAnimation(
            relative_path=relative.as_posix(),
            mime_type=mime_type,
            extension=extension,
            byte_count=len(content),
        )

    def read(self, relative_path: str) -> Path:
        path = self._resolve(relative_path)
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        return path

    def read_bytes(self, relative_path: str) -> bytes:
        return self.read(relative_path).read_bytes()

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
        if path.exists():
            shutil.rmtree(path)

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
