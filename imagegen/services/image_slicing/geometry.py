from __future__ import annotations

import io
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .models import MAX_SLICES, MIN_SLICE_SIDE, SliceBox


def grid_boxes(
    width: int,
    height: int,
    *,
    rows: int,
    columns: int,
) -> list[SliceBox]:
    if not 1 <= rows <= 8 or not 1 <= columns <= 8 or rows * columns > MAX_SLICES:
        raise ValueError("切片行列数无效")
    if min(width, height) < MIN_SLICE_SIDE:
        raise ValueError("原图尺寸过小")
    if width < columns * MIN_SLICE_SIDE or height < rows * MIN_SLICE_SIDE:
        raise ValueError("行列数过多，没有足够的切片空间")

    x_edges = [_grid_edge(width, index, columns) for index in range(columns + 1)]
    y_edges = [_grid_edge(height, index, rows) for index in range(rows + 1)]
    boxes = []
    for row in range(rows):
        for column in range(columns):
            x = x_edges[column]
            y = y_edges[row]
            boxes.append(
                SliceBox(
                    x=x,
                    y=y,
                    width=x_edges[column + 1] - x_edges[column],
                    height=y_edges[row + 1] - y_edges[row],
                )
            )
    return boxes


def _grid_edge(size: int, index: int, count: int) -> int:
    return (size * index + count // 2) // count


def validate_boxes(value: object, *, width: int, height: int) -> list[SliceBox]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SLICES:
        raise ValueError(f"请选择 1 到 {MAX_SLICES} 个切片")
    boxes = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("切片坐标无效")
        try:
            box = SliceBox(
                x=_integer_coordinate(raw["x"]),
                y=_integer_coordinate(raw["y"]),
                width=_integer_coordinate(raw["width"]),
                height=_integer_coordinate(raw["height"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("切片坐标无效") from exc
        if (
            box.x < 0
            or box.y < 0
            or box.width < MIN_SLICE_SIDE
            or box.height < MIN_SLICE_SIDE
            or box.x + box.width > width
            or box.y + box.height > height
        ):
            raise ValueError("切片超出原图范围或尺寸过小")
        boxes.append(box)
    if sum(box.width * box.height for box in boxes) > width * height:
        raise ValueError("切片总面积不能超过原图")
    for index, box in enumerate(boxes):
        if any(_boxes_overlap(box, other) for other in boxes[index + 1 :]):
            raise ValueError("切片不能相互重叠")
    return boxes


def crop_pngs(path: str | Path, boxes: Iterable[SliceBox]) -> list[tuple[str, bytes]]:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        image.load()
        results = []
        for index, box in enumerate(boxes, start=1):
            crop = image.crop((box.x, box.y, box.x + box.width, box.y + box.height))
            if crop.mode not in {"RGB", "RGBA"}:
                crop = crop.convert("RGBA" if "A" in crop.getbands() else "RGB")
            output = io.BytesIO()
            crop.save(output, format="PNG", optimize=True)
            results.append((f"slice_{index:02d}_{box.width}x{box.height}.png", output.getvalue()))
    return results


def image_size(path: str | Path) -> tuple[int, int]:
    with Image.open(path) as source:
        return ImageOps.exif_transpose(source).size


def _boxes_overlap(first: SliceBox, second: SliceBox) -> bool:
    return (
        first.x < second.x + second.width
        and second.x < first.x + first.width
        and first.y < second.y + second.height
        and second.y < first.y + first.height
    )


def _integer_coordinate(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    raise ValueError
