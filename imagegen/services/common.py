from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..errors import ServiceError

MONEY_QUANTUM = Decimal("0.0001")
IMAGE_SIZE_PATTERN = re.compile(r"^([1-9]\d{1,4})x([1-9]\d{1,4})$")
IMAGE_DIMENSION_MIN = 64
IMAGE_DIMENSION_MAX = 8192


def money(value: Decimal | str | int | float) -> Decimal:
    try:
        return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
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
