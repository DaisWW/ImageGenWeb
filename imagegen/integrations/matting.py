from __future__ import annotations

import io
import warnings
from dataclasses import dataclass

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
    """HTTP client for the Lucida /remove service.

    Disabled when base_url is empty. Used for transparent-background generation
    post-processing and optional explicit downloads.
    """

    base_url: str = ""
    model: str = "lucida"
    timeout_seconds: float = 120.0
    session: requests.Session | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url.strip())

    def healthcheck(self) -> None:
        if not self.enabled:
            return
        session = self.session or requests.Session()
        try:
            response = session.get(
                f"{self.base_url.rstrip('/')}/ready",
                timeout=(2, 5),
            )
        except requests.RequestException as exc:
            raise ServiceError(
                "Lucida 抠图服务未就绪",
                code="matting_unavailable",
                status_code=503,
            ) from exc
        if response.status_code >= 400:
            raise ServiceError(
                "Lucida 抠图服务未就绪",
                code="matting_unavailable",
                status_code=503,
            )

    def remove_background(self, content: bytes, *, filename: str = "image.png") -> bytes:
        if not self.enabled:
            raise ServiceError(
                "Lucida 抠图服务未配置（请设置 LUCIDA_MATTING_URL）",
                code="matting_unavailable",
                status_code=503,
            )
        if not content:
            raise ServiceError("图片内容为空", code="invalid_image")
        if len(content) > MAX_MATTING_BYTES:
            raise ServiceError(
                "图片超过 50 MiB 限制，无法发送到 Lucida",
                code="matting_input_too_large",
            )
        _assert_safe_input(content)

        url = f"{self.base_url.rstrip('/')}/remove"
        params = {
            "model": self.model or "lucida",
            "decontaminate": "true",
        }
        files = {"file": (filename or "image.png", content, "application/octet-stream")}
        session = self.session or requests.Session()
        try:
            response = session.post(
                url,
                params=params,
                files=files,
                timeout=(10, float(self.timeout_seconds)),
            )
        except requests.Timeout as exc:
            raise ServiceError(
                "Lucida 抠图超时",
                code="matting_timeout",
                status_code=504,
            ) from exc
        except requests.RequestException as exc:
            raise ServiceError(
                "无法连接 Lucida 抠图服务",
                code="matting_connection_failed",
                status_code=503,
            ) from exc

        if response.status_code >= 400:
            detail = _response_detail(response)
            raise ServiceError(
                detail or f"Lucida 抠图失败（HTTP {response.status_code}）",
                code="matting_upstream_failed",
                status_code=502,
            )

        result = response.content or b""
        if len(result) > MAX_MATTING_BYTES:
            raise ServiceError(
                "Lucida 返回图片超过 50 MiB 限制",
                code="matting_output_too_large",
                status_code=502,
            )
        _assert_real_alpha_png(result)
        return result


def _assert_safe_input(content: bytes) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if width * height > MAX_MATTING_PIXELS:
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


def _assert_real_alpha_png(content: bytes) -> None:
    if not content:
        raise ServiceError(
            "Lucida 返回空结果",
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
                        "Lucida 返回图片像素数量超过安全限制",
                        code="matting_output_too_large",
                        status_code=502,
                    )
                image.load()
                if image.format != "PNG":
                    raise ServiceError(
                        "Lucida 未返回 PNG 结果",
                        code="matting_invalid_result",
                        status_code=502,
                    )
                alpha_extrema = image.convert("RGBA").getchannel("A").getextrema()
    except ServiceError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ServiceError(
            "Lucida 返回图片像素数量超过安全限制",
            code="matting_output_too_large",
            status_code=502,
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ServiceError(
            "Lucida 返回的图片无效",
            code="matting_invalid_result",
            status_code=502,
        ) from exc

    if alpha_extrema[0] == 255:
        raise ServiceError(
            "Lucida 未返回真实透明背景图片",
            code="matting_opaque_result",
            status_code=502,
        )


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


def _response_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:200]
    if isinstance(payload, dict):
        for key in ("detail", "error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    return ""
