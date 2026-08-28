from __future__ import annotations

import base64
import io
import json
import warnings
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import requests
from PIL import Image, UnidentifiedImageError

from ..errors import ServiceError

MAX_MATTING_BYTES = 50 * 1024 * 1024
MAX_MATTING_PIXELS = 40_000_000
CHECKERBOARD_SAMPLE_SIDE = 256
CHECKERBOARD_MIN_SIDE = 16
CHECKERBOARD_BORDER_RATIO = 0.2
CHECKERBOARD_MAX_SAME_DISTANCE = 0.07
CHECKERBOARD_MIN_OPPOSITE_DISTANCE = 0.08
CHECKERBOARD_MIN_MATCH_RATIO = 0.72
CHECKERBOARD_MAX_SAMPLES = 1200


@dataclass(frozen=True)
class LucidaMattingClient:
    """HTTP client for Lucida-compatible background-removal services."""

    adapter_id = "lucida"
    base_url: str = ""
    model: str = "lucida"
    timeout_seconds: float = 120.0
    session: requests.Session | None = None
    decontaminate: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.base_url.strip())

    def healthcheck(self) -> None:
        if not self.enabled:
            return
        session = self.session or requests.Session()
        response = None
        try:
            response = session.get(
                f"{self.base_url.rstrip('/')}/ready",
                timeout=(1, 1),
            )
            if response.status_code >= 400:
                raise ServiceError(
                    "透明化服务未就绪",
                    code="matting_unavailable",
                    status_code=503,
                )
        except requests.RequestException as exc:
            raise ServiceError(
                "透明化服务未就绪",
                code="matting_unavailable",
                status_code=503,
            ) from exc
        finally:
            if response is not None:
                _close_response(response)

    def close(self) -> None:
        # Sessions are owned by MattingAdapterFactory and closed there.
        return None

    def remove_background(self, content: bytes, *, filename: str = "image.png") -> bytes:
        if not self.enabled:
            raise ServiceError(
                "透明化服务未配置",
                code="matting_unavailable",
                status_code=503,
            )
        if not content:
            raise ServiceError("图片内容为空", code="invalid_image")
        if len(content) > MAX_MATTING_BYTES:
            raise ServiceError(
                "图片超过 50 MiB 限制，无法发送到透明化服务",
                code="matting_input_too_large",
            )
        _assert_safe_input(content)

        url = f"{self.base_url.rstrip('/')}/remove"
        params = {
            "model": self.model or "lucida",
            "decontaminate": "true" if self.decontaminate else "false",
        }
        files = {"file": (filename or "image.png", content, "application/octet-stream")}
        session = self.session or requests.Session()
        response = None
        try:
            response = session.post(
                url,
                params=params,
                files=files,
                timeout=(10, float(self.timeout_seconds)),
            )
        except requests.Timeout as exc:
            raise ServiceError(
                "透明化处理超时",
                code="matting_timeout",
                status_code=504,
            ) from exc
        except requests.RequestException as exc:
            raise ServiceError(
                "无法连接透明化服务",
                code="matting_connection_failed",
                status_code=503,
            ) from exc
        try:
            if response.status_code >= 400:
                raise ServiceError(
                    f"透明化处理失败（HTTP {response.status_code}）",
                    code="matting_upstream_failed",
                    status_code=502,
                )

            result = response.content or b""
            if len(result) > MAX_MATTING_BYTES:
                raise ServiceError(
                    "透明化服务返回图片超过 50 MiB 限制",
                    code="matting_output_too_large",
                    status_code=502,
                )
            _assert_real_alpha_png(result)
            return result
        finally:
            if response is not None:
                _close_response(response)


@dataclass(frozen=True)
class GenericMattingClient:
    """HTTP adapter for rembg/BiRefNet-style multipart image services."""

    adapter_id = "generic_http"
    base_url: str = ""
    model: str = ""
    timeout_seconds: float = 120.0
    remove_path: str = "/remove"
    health_path: str = "/health"
    model_param: str = "model"
    file_field: str = "file"
    response_field: str = ""
    decontaminate: bool = False
    session: requests.Session | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url.strip())

    def healthcheck(self) -> None:
        if not self.enabled:
            raise ServiceError(
                "透明化服务未配置",
                code="matting_unavailable",
                status_code=503,
            )
        health_path = str(self.health_path or "").strip()
        if not health_path:
            return

        session = self.session or requests.Session()
        response = None
        try:
            response = session.get(
                _join_url(self.base_url, health_path),
                timeout=(2, min(float(self.timeout_seconds), 10.0)),
            )
            if response.status_code >= 400:
                raise ServiceError(
                    "透明化服务未就绪",
                    code="matting_unavailable",
                    status_code=503,
                )
        except requests.RequestException as exc:
            raise ServiceError(
                "透明化服务未就绪",
                code="matting_unavailable",
                status_code=503,
            ) from exc
        finally:
            if response is not None:
                _close_response(response)

    def close(self) -> None:
        # Sessions are owned by MattingAdapterFactory and closed there.
        return None

    def remove_background(self, content: bytes, *, filename: str = "image.png") -> bytes:
        if not self.enabled:
            raise ServiceError(
                "透明化服务未配置",
                code="matting_unavailable",
                status_code=503,
            )
        if not content:
            raise ServiceError("图片内容为空", code="invalid_image")
        if len(content) > MAX_MATTING_BYTES:
            raise ServiceError(
                "图片超过 50 MiB 限制，无法发送到透明化服务",
                code="matting_input_too_large",
            )
        _assert_safe_input(content)
        params: dict[str, str] = {}
        if self.model and self.model_param:
            params[self.model_param] = self.model
        if self.decontaminate:
            params["decontaminate"] = "true"
        files = {
            self.file_field or "file": (
                filename or "image.png",
                content,
                "application/octet-stream",
            )
        }
        session = self.session or requests.Session()
        response = None
        try:
            response = session.post(
                _join_url(self.base_url, self.remove_path),
                params=params,
                files=files,
                timeout=(10, float(self.timeout_seconds)),
            )
        except requests.Timeout as exc:
            raise ServiceError(
                "透明化处理超时",
                code="matting_timeout",
                status_code=504,
            ) from exc
        except requests.RequestException as exc:
            raise ServiceError(
                "无法连接透明化服务",
                code="matting_connection_failed",
                status_code=503,
            ) from exc
        try:
            if response.status_code >= 400:
                raise ServiceError(
                    f"透明化处理失败（HTTP {response.status_code}）",
                    code="matting_upstream_failed",
                    status_code=502,
                )
            raw_result = response.content or b""
            if len(raw_result) > MAX_MATTING_BYTES:
                raise ServiceError(
                    "透明化服务返回图片超过 50 MiB 限制",
                    code="matting_output_too_large",
                    status_code=502,
                )
            result = _decode_response_field(raw_result, self.response_field)
            if len(result) > MAX_MATTING_BYTES:
                raise ServiceError(
                    "透明化服务返回图片超过 50 MiB 限制",
                    code="matting_output_too_large",
                    status_code=502,
                )
            return _normalize_alpha_png(result)
        finally:
            if response is not None:
                _close_response(response)


# Common naming used by integrations that expose rembg-compatible HTTP APIs.
RembgMattingClient = GenericMattingClient


def _assert_safe_input(content: bytes, *, max_pixels: int = MAX_MATTING_PIXELS) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if width * height > max_pixels:
                    raise ServiceError(
                        "图片像素数量超过安全限制",
                        code="matting_input_too_large",
                    )
                image.load()
    except ServiceError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ServiceError(
            "图片像素数量超过安全限制",
            code="matting_input_too_large",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ServiceError("文件不是有效图片", code="invalid_image") from exc


def validate_matting_output(
    content: bytes,
    *,
    expected_size: tuple[int, int] | None = None,
) -> None:
    """Validate the normalized output contract shared by every adapter."""
    _assert_real_alpha_png(content, expected_size=expected_size)


def _assert_real_alpha_png(
    content: bytes,
    *,
    expected_size: tuple[int, int] | None = None,
) -> None:
    if not content:
        raise ServiceError(
            "透明化服务返回空结果",
            code="matting_invalid_result",
            status_code=502,
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if width * height > MAX_MATTING_PIXELS:
                    raise ServiceError(
                        "透明化服务返回图片像素数量超过安全限制",
                        code="matting_output_too_large",
                        status_code=502,
                    )
                if expected_size is not None and (width, height) != expected_size:
                    raise ServiceError(
                        "透明化服务返回图片尺寸与原图不一致",
                        code="matting_dimension_mismatch",
                        status_code=502,
                    )
                image.load()
                if image.format != "PNG":
                    raise ServiceError(
                        "透明化服务未返回 PNG 结果",
                        code="matting_invalid_result",
                        status_code=502,
                    )
                alpha_extrema = image.convert("RGBA").getchannel("A").getextrema()
    except ServiceError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ServiceError(
            "透明化服务返回图片像素数量超过安全限制",
            code="matting_output_too_large",
            status_code=502,
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ServiceError(
            "透明化服务返回的图片无效",
            code="matting_invalid_result",
            status_code=502,
        ) from exc

    if alpha_extrema[0] == 255:
        raise ServiceError(
            "透明化服务未返回真实透明背景图片",
            code="matting_opaque_result",
            status_code=502,
        )


def _normalize_alpha_png(content: bytes) -> bytes:
    """Convert a valid RGBA result to deterministic PNG while preserving alpha."""
    if not content:
        raise ServiceError(
            "透明化服务返回空结果",
            code="matting_invalid_result",
            status_code=502,
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if width * height > MAX_MATTING_PIXELS:
                    raise ServiceError(
                        "透明化服务返回图片像素数量超过安全限制",
                        code="matting_output_too_large",
                        status_code=502,
                    )
                image.load()
                rgba = image.convert("RGBA")
                alpha_extrema = rgba.getchannel("A").getextrema()
                if alpha_extrema[0] == 255:
                    raise ServiceError(
                        "透明化服务未返回真实透明背景图片",
                        code="matting_opaque_result",
                        status_code=502,
                    )
                stream = io.BytesIO()
                try:
                    rgba.save(stream, format="PNG", compress_level=6, optimize=False)
                    encoded = stream.getvalue()
                finally:
                    rgba.close()
                    stream.close()
                if len(encoded) > MAX_MATTING_BYTES:
                    raise ServiceError(
                        "透明化服务返回图片超过 50 MiB 限制",
                        code="matting_output_too_large",
                        status_code=502,
                    )
                return encoded
    except ServiceError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ServiceError(
            "透明化服务返回图片像素数量超过安全限制",
            code="matting_output_too_large",
            status_code=502,
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ServiceError(
            "透明化服务返回的图片无效",
            code="matting_invalid_result",
            status_code=502,
        ) from exc


def _join_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    normalized = "/" + str(PurePosixPath(path or "/")).lstrip("/")
    return f"{base}{normalized}"


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _decode_response_field(content: bytes, field: str) -> bytes:
    """Extract an optional JSON/base64 response field without fetching URLs."""
    field = str(field or "").strip()
    if not field:
        return content
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError(
            "透明化服务返回的 JSON 结果无效",
            code="matting_invalid_result",
            status_code=502,
        ) from exc
    value: Any = payload
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ServiceError(
                "透明化服务返回结果缺少图片字段",
                code="matting_invalid_result",
                status_code=502,
            )
        value = value[part]
    if not isinstance(value, str):
        raise ServiceError(
            "透明化服务返回图片字段格式无效",
            code="matting_invalid_result",
            status_code=502,
        )
    encoded = value.strip()
    if encoded.startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or ";base64" not in header.lower():
            raise ServiceError(
                "透明化服务返回图片字段格式无效",
                code="matting_invalid_result",
                status_code=502,
            )
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ServiceError(
            "透明化服务返回图片字段不是有效的 Base64",
            code="matting_invalid_result",
            status_code=502,
        ) from exc


def image_has_real_alpha(content: bytes) -> bool:
    """True when the image already has non-opaque alpha pixels."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if width * height > MAX_MATTING_PIXELS:
                    return False
                image.load()
                if "A" not in image.getbands() and image.mode not in {"RGBA", "LA", "PA"}:
                    return False
                alpha_extrema = image.convert("RGBA").getchannel("A").getextrema()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return False
    except (UnidentifiedImageError, OSError, ValueError):
        return False
    return alpha_extrema[0] < 255


def image_has_baked_checkerboard(content: bytes) -> bool:
    """True when visible pixels contain a high-confidence checkerboard pattern."""
    if not content:
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                source.load()
                rgba = source.convert("RGBA")
                try:
                    width, height = rgba.size
                    scale = min(1.0, CHECKERBOARD_SAMPLE_SIDE / max(width, height))
                    if scale < 1:
                        sample = rgba.resize(
                            (
                                max(CHECKERBOARD_MIN_SIDE, round(width * scale)),
                                max(CHECKERBOARD_MIN_SIDE, round(height * scale)),
                            ),
                            Image.Resampling.NEAREST,
                        )
                    else:
                        sample = rgba
                    try:
                        return _sample_has_checkerboard(sample)
                    finally:
                        if sample is not rgba:
                            sample.close()
                finally:
                    rgba.close()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return False
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def _sample_has_checkerboard(image: Image.Image) -> bool:
    width, height = image.size
    if min(width, height) < CHECKERBOARD_MIN_SIDE:
        return False
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        pixels = list(image.getdata())
    border = max(2, round(min(width, height) * CHECKERBOARD_BORDER_RATIO))
    visible = [pixel[3] >= 245 for pixel in pixels]
    border_coordinates = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if _in_checkerboard_border(x, y, width, height, border)
    ]
    if len(border_coordinates) > CHECKERBOARD_MAX_SAMPLES:
        stride = (
            len(border_coordinates) + CHECKERBOARD_MAX_SAMPLES - 1
        ) // CHECKERBOARD_MAX_SAMPLES
        border_coordinates = border_coordinates[::stride]
    max_period = min(width, height) // 3
    for period in range(2, max_period + 1):
        direction_matches = []
        for dx, dy in ((period, 0), (0, period)):
            same_distances: list[float] = []
            opposite_distances: list[float] = []
            for x, y in border_coordinates:
                opposite_x = x + dx
                opposite_y = y + dy
                same_x = x + 2 * dx
                same_y = y + 2 * dy
                if (
                    same_x >= width
                    or same_y >= height
                    or not _in_checkerboard_border(opposite_x, opposite_y, width, height, border)
                    or not _in_checkerboard_border(same_x, same_y, width, height, border)
                ):
                    continue
                source_index = y * width + x
                opposite_index = opposite_y * width + opposite_x
                same_index = same_y * width + same_x
                if not (visible[source_index] and visible[opposite_index] and visible[same_index]):
                    continue
                same_distances.append(_pixel_distance(pixels[source_index], pixels[same_index]))
                opposite_distances.append(
                    _pixel_distance(pixels[source_index], pixels[opposite_index])
                )
            if len(same_distances) < 40:
                break
            same_match = sum(
                distance <= CHECKERBOARD_MAX_SAME_DISTANCE for distance in same_distances
            ) / len(same_distances)
            opposite_match = sum(
                distance >= CHECKERBOARD_MIN_OPPOSITE_DISTANCE for distance in opposite_distances
            ) / len(opposite_distances)
            direction_matches.append(
                same_match >= CHECKERBOARD_MIN_MATCH_RATIO
                and opposite_match >= CHECKERBOARD_MIN_MATCH_RATIO
            )
        if len(direction_matches) == 2 and all(direction_matches):
            return True
    return False


def _in_checkerboard_border(x: int, y: int, width: int, height: int, border: int) -> bool:
    return x < border or y < border or x >= width - border or y >= height - border


def _pixel_distance(first: tuple[int, ...], second: tuple[int, ...]) -> float:
    return sum((left - right) ** 2 for left, right in zip(first[:3], second[:3])) ** 0.5 / 441.673
