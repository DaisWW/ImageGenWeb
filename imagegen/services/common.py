from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..errors import ServiceError

MONEY_QUANTUM = Decimal("0.0001")
IMAGE_SIZE_PATTERN = re.compile(r"^([1-9]\d{1,4})x([1-9]\d{1,4})$")
IMAGE_DIMENSION_MIN = 64
IMAGE_DIMENSION_MAX = 8192
CANVAS_RATIO_PATTERN = re.compile(r"^([1-9]\d{0,3}):([1-9]\d{0,3})$")


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
    match = IMAGE_SIZE_PATTERN.fullmatch(size)
    if not match or any(
        not IMAGE_DIMENSION_MIN <= int(dimension) <= IMAGE_DIMENSION_MAX
        for dimension in match.groups()
    ):
        raise ServiceError("尺寸格式应为宽x高，单边范围 64–8192 像素")
    return size


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
    width, height = (int(part) for part in normalized_size.split("x", 1))
    if "width" in request and "height" in request:
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
