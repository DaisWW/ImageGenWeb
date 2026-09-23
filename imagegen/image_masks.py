from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from .storage import MAX_IMAGE_DIMENSION, MAX_IMAGE_PIXELS

MAX_MASK_BYTES = 4 * 1024 * 1024
MASK_ALPHA_THRESHOLD = 128


class InvalidMaskError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GenerationMask:
    """Canonical binary-alpha PNG used by one masked generation job."""

    content: bytes
    byte_count: int
    width: int
    height: int
    sha256: str
    editable_ratio: float

    @classmethod
    def from_upload(
        cls,
        content: bytes,
        *,
        reference_size: tuple[int, int],
    ) -> GenerationMask:
        if not content:
            raise InvalidMaskError("蒙版内容为空")
        if len(content) > MAX_MASK_BYTES:
            raise InvalidMaskError("蒙版不能超过 4 MiB")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as source:
                    if (source.format or "").upper() != "PNG":
                        raise InvalidMaskError("蒙版必须是 PNG 图片")
                    if getattr(source, "n_frames", 1) > 1:
                        raise InvalidMaskError("蒙版必须是静态 PNG 图片")
                    width, height = source.size
                    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                        raise InvalidMaskError(f"蒙版单边不能超过 {MAX_IMAGE_DIMENSION} 像素")
                    if width * height > MAX_IMAGE_PIXELS:
                        raise InvalidMaskError(f"蒙版总像素不能超过 {MAX_IMAGE_PIXELS:,}")
                    if not _same_aspect_ratio((width, height), reference_size):
                        raise InvalidMaskError("蒙版比例必须与局部重绘原图一致")
                    image = source.convert("RGBA")
                    image.load()
        except InvalidMaskError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise InvalidMaskError("蒙版像素数量超过安全限制") from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise InvalidMaskError("蒙版不是有效的 PNG 图片") from exc

        try:
            alpha = image.getchannel("A").point(
                lambda value: 0 if value < MASK_ALPHA_THRESHOLD else 255
            )
            minimum, maximum = alpha.getextrema()
            if minimum == maximum == 255:
                raise InvalidMaskError("尚未选择需要局部重绘的区域")
            histogram = alpha.histogram()
            editable_pixels = int(histogram[0])
            normalized = Image.new("RGBA", image.size, (255, 255, 255, 255))
            normalized.putalpha(alpha)
            stream = io.BytesIO()
            normalized.save(stream, format="PNG", optimize=True)
            canonical = stream.getvalue()
        finally:
            image.close()
            if "alpha" in locals():
                alpha.close()
            if "normalized" in locals():
                normalized.close()

        if len(canonical) > MAX_MASK_BYTES:
            raise InvalidMaskError("规范化后的蒙版不能超过 4 MiB")
        return cls(
            content=canonical,
            byte_count=len(canonical),
            width=width,
            height=height,
            sha256=hashlib.sha256(canonical).hexdigest(),
            editable_ratio=editable_pixels / (width * height),
        )


def _same_aspect_ratio(
    actual: tuple[int, int],
    expected: tuple[int, int],
) -> bool:
    width, height = actual
    expected_width, expected_height = expected
    if min(width, height, expected_width, expected_height) <= 0:
        return False
    # Allow one-pixel rounding when the browser renders a bounded editor canvas.
    return abs(width * expected_height - height * expected_width) <= max(
        expected_width,
        expected_height,
    )
