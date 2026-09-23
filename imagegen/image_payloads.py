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


def visual_image_size(content: bytes) -> tuple[int, int]:
    """Return the dimensions users see after applying EXIF orientation."""

    try:
        with Image.open(io.BytesIO(content)) as source:
            oriented = ImageOps.exif_transpose(source)
            return oriented.size
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise ValueError("局部重绘原图无效") from exc


def prepare_masked_image_bytes(
    content: bytes,
    mime_type: str,
    mask_content: bytes,
    *,
    max_side: int = IMAGE_MAX_SIDE,
    quality: int = 95,
) -> tuple[bytes, str, bytes]:
    """Normalize the first edit image and mask with identical geometry."""

    source = None
    mask = None
    normalized_mask = None
    alpha = None
    try:
        with Image.open(io.BytesIO(content)) as opened_source:
            source = ImageOps.exif_transpose(opened_source).copy()
        with Image.open(io.BytesIO(mask_content)) as opened_mask:
            mask = opened_mask.convert("RGBA")
            mask.load()

        source.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        has_alpha = "A" in source.getbands() or "transparency" in source.info
        converted_source = source.convert("RGBA" if has_alpha else "RGB")
        source.close()
        source = converted_source
        if mask.size != source.size:
            resized_mask = mask.resize(source.size, Image.Resampling.NEAREST)
            mask.close()
            mask = resized_mask

        alpha = mask.getchannel("A").point(lambda value: 0 if value < 128 else 255)
        normalized_mask = Image.new("RGBA", source.size, (255, 255, 255, 255))
        normalized_mask.putalpha(alpha)

        source_stream = io.BytesIO()
        source.save(
            source_stream,
            format="WEBP",
            quality=quality,
            method=4,
            lossless=has_alpha,
        )
        mask_stream = io.BytesIO()
        normalized_mask.save(mask_stream, format="PNG", optimize=True)
        return source_stream.getvalue(), "image/webp", mask_stream.getvalue()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise ValueError("局部重绘图片或蒙版无效") from exc
    finally:
        for image in (source, mask, normalized_mask, alpha):
            if image is not None:
                image.close()
