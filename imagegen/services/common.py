from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..errors import ServiceError

MONEY_QUANTUM = Decimal("0.0001")
IMAGE_SIZE_PATTERN = re.compile(r"^([1-9]\d{1,4})x([1-9]\d{1,4})$")
IMAGE_SIZE_AUTO = "auto"
IMAGE_DIMENSION_MIN = 16
IMAGE_DIMENSION_MAX = 3840
IMAGE_MIN_PIXELS = 655_360
IMAGE_MAX_PIXELS = 8_294_400
IMAGE_MAX_ASPECT_RATIO = 3
CANVAS_RATIO_PATTERN = re.compile(r"^([1-9]\d{0,3}):([1-9]\d{0,3})$")
GPT_IMAGE_2_MODEL_PATTERN = re.compile(r"^gpt-image-2(?:$|[.-])", re.IGNORECASE)


def is_gpt_image_2_model(value: Any) -> bool:
    """Return whether a model identifier belongs to the GPT Image 2 family."""
    return bool(GPT_IMAGE_2_MODEL_PATTERN.match(str(value or "").strip()))


def money(value: Decimal | str | int | float) -> Decimal:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
        return amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ServiceError("金额格式无效") from exc


def normalize_image_size(value: Any) -> str:
    size = str(value).strip().lower().replace("×", "x")
    if size == IMAGE_SIZE_AUTO:
        return size
    match = IMAGE_SIZE_PATTERN.fullmatch(size)
    if not match or not _valid_image_dimensions(*(int(dimension) for dimension in match.groups())):
        raise ServiceError(
            "尺寸格式必须为 auto 或宽x高；宽高须为 16 的倍数，比例不超过 3:1，"
            "像素数需在 655,360 到 8,294,400，单边不超过 3840"
        )
    return size


def _valid_image_dimensions(width: int, height: int) -> bool:
    if not (
        IMAGE_DIMENSION_MIN <= width <= IMAGE_DIMENSION_MAX
        and IMAGE_DIMENSION_MIN <= height <= IMAGE_DIMENSION_MAX
    ):
        return False
    if width % 16 or height % 16:
        return False
    if width * height < IMAGE_MIN_PIXELS or width * height > IMAGE_MAX_PIXELS:
        return False
    return max(width, height) <= IMAGE_MAX_ASPECT_RATIO * min(width, height)


def valid_image_size(value: Any) -> bool:
    size = str(value).strip().lower().replace("×", "x")
    if size == IMAGE_SIZE_AUTO:
        return True
    match = IMAGE_SIZE_PATTERN.fullmatch(size)
    return bool(match) and _valid_image_dimensions(
        int(match.group(1)),
        int(match.group(2)),
    )


def normalize_canvas_request(value: Any) -> dict[str, Any]:
    """Normalize an optional, user-stated canvas request from a prompt draft."""
    if not isinstance(value, dict):
        return {}
    width = _canvas_dimension(value.get("width"))
    height = _canvas_dimension(value.get("height"))
    ratio = _canvas_ratio(value.get("aspect_ratio"))
    if width and height:
        derived_ratio = _ratio_for_dimensions(width, height)
        if ratio and ratio != derived_ratio:
            return {}
        return {
            "width": width,
            "height": height,
            "aspect_ratio": derived_ratio,
        }
    return {"aspect_ratio": ratio} if ratio else {}


def canvas_request_conflicts(value: Any, size: Any) -> bool:
    request = normalize_canvas_request(value)
    if not request:
        return False
    try:
        normalized_size = normalize_image_size(size)
    except ServiceError:
        return False
    if normalized_size == IMAGE_SIZE_AUTO:
        return False
    width, height = (int(part) for part in normalized_size.split("x", 1))
    if "width" in request and "height" in request:
        if not valid_image_size(f"{request['width']}x{request['height']}"):
            return request["aspect_ratio"] != _ratio_for_dimensions(width, height)
        return (request["width"], request["height"]) != (width, height)
    return request.get("aspect_ratio") != _ratio_for_dimensions(width, height)


def _canvas_dimension(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not number.is_finite() or number != number.to_integral_value():
        return None
    dimension = int(number)
    return dimension if IMAGE_DIMENSION_MIN <= dimension <= IMAGE_DIMENSION_MAX else None


def _canvas_ratio(value: Any) -> str:
    match = CANVAS_RATIO_PATTERN.fullmatch(str(value or "").strip().replace("：", ":"))
    if not match:
        return ""
    return _ratio_for_dimensions(int(match.group(1)), int(match.group(2)))


def _ratio_for_dimensions(width: int, height: int) -> str:
    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"
