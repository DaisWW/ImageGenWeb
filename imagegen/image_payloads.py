from __future__ import annotations

import io
from pathlib import PurePath

from PIL import Image, ImageOps, UnidentifiedImageError

IMAGE_MAX_SIDE = 1280
IMAGE_WEBP_QUALITY = 85
IMAGE_COMPRESSION_THRESHOLD_BYTES = 256 * 1024


def prepare_image_bytes(
    content: bytes,
    mime_type: str,
    *,
    max_side: int = IMAGE_MAX_SIDE,
    quality: int = IMAGE_WEBP_QUALITY,
    compression_threshold: int = IMAGE_COMPRESSION_THRESHOLD_BYTES,
) -> tuple[bytes, str]:
    """Return a bounded image representation suitable for model requests."""
    if not content:
        return content, mime_type

    image = None
    try:
        with Image.open(io.BytesIO(content)) as source:
            width, height = source.size
            image = ImageOps.exif_transpose(source).copy()
        needs_resize = max(width, height) > max_side
        if len(content) <= compression_threshold and not needs_resize:
            return content, mime_type

        has_alpha = "A" in image.getbands() or "transparency" in image.info
        image = image.convert("RGBA" if has_alpha else "RGB")
        if needs_resize:
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

        output = io.BytesIO()
        image.save(output, format="WEBP", quality=quality, method=4)
        compressed = output.getvalue()
        if compressed and (needs_resize or len(compressed) < len(content)):
            return compressed, "image/webp"
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ):
        pass
    finally:
        if image is not None:
            image.close()
    return content, mime_type


def prepared_filename(filename: str, mime_type: str) -> str:
    if mime_type != "image/webp":
        return filename
    path = PurePath(filename or "image")
    return f"{path.stem or 'image'}.webp"
