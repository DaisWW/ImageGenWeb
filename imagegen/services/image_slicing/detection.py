from __future__ import annotations

import re
from pathlib import Path
from statistics import median

from PIL import Image, ImageOps

from .geometry import grid_boxes
from .models import ANALYSIS_MAX_SIDE, AxisLayout

MIN_VISUAL_COVERAGE = 0.35
MIN_HINTED_COVERAGE = 0.18
PROMPT_FALLBACK_SCORE = 0.50
MIN_BOUNDARY_CONTRAST = 1.25
MIN_VISIBLE_RATIO_FOR_VISUAL_DETECTION = 0.55
MIN_PERIODIC_SCORE = 0.45
MIN_PERIODIC_ACTIVITY = 0.006
MAX_PERIODIC_BOUNDARY_RATIO = 0.72
MIN_PERIODIC_UNIFORMITY = 0.70


def analyze_image(path: str | Path, *, prompt: str = "") -> dict[str, object]:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        width, height = image.size
        image.thumbnail((ANALYSIS_MAX_SIDE, ANALYSIS_MAX_SIDE), Image.Resampling.LANCZOS)
        sample = image.convert("RGBA")
        sample.load()

    hint = _grid_hint(prompt)
    sparse_transparency = _has_sparse_transparency(sample)
    x_layout = _detect_axis(
        sample,
        axis=0,
        hinted_count=hint[1] if hint else None,
        sparse_transparency=sparse_transparency,
    )
    y_layout = _detect_axis(
        sample,
        axis=1,
        hinted_count=hint[0] if hint else None,
        sparse_transparency=sparse_transparency,
    )
    if hint and not sparse_transparency:
        hinted_rows, hinted_columns = hint
        if x_layout.count == 1:
            x_layout = AxisLayout(count=hinted_columns, score=PROMPT_FALLBACK_SCORE)
        if y_layout.count == 1:
            y_layout = AxisLayout(count=hinted_rows, score=PROMPT_FALLBACK_SCORE)

    rows = y_layout.count
    columns = x_layout.count
    boxes = grid_boxes(width, height, rows=rows, columns=columns)
    active_scores = [layout.score for layout in (x_layout, y_layout) if layout.count > 1]
    detected = len(boxes) > 1
    confidence_score = min(active_scores) if active_scores else 0.0
    confidence = (
        "high"
        if detected and confidence_score >= 0.72
        else "medium"
        if detected and confidence_score >= 0.45
        else "low"
    )

    return {
        "width": width,
        "height": height,
        "detected": detected,
        "confidence": confidence,
        "rows": rows,
        "columns": columns,
        "boxes": [
            box.as_dict(row=index // columns, column=index % columns)
            for index, box in enumerate(boxes)
        ],
    }


def _detect_axis(
    image: Image.Image,
    *,
    axis: int,
    hinted_count: int | None,
    sparse_transparency: bool = False,
) -> AxisLayout:
    background_layout = _background_axis_layout(image, axis=axis)
    if background_layout is not None:
        return background_layout
    if sparse_transparency:
        return AxisLayout(count=1, score=0.0)

    edge, coverage = _axis_profiles(image, axis)
    length = len(edge)
    window = max(3, round(length * 0.015))
    best = AxisLayout(count=1, score=0.0)

    for count in range(2, 9):
        if length < count * 8:
            continue
        boundary_coverage = []
        boundary_edge = []
        for index in range(1, count):
            position = round(length * index / count)
            boundary_coverage.append(_window_max(coverage, position, window))
            boundary_edge.append(_window_max(edge, position, window))

        minimum_coverage = min(boundary_coverage)
        required_coverage = MIN_HINTED_COVERAGE if hinted_count == count else MIN_VISUAL_COVERAGE
        if minimum_coverage < required_coverage:
            continue

        average_coverage = sum(boundary_coverage) / len(boundary_coverage)
        minimum_edge = min(boundary_edge)
        interior_edge = _cell_interior_median(edge, length=length, count=count)
        interior_coverage = _cell_interior_median(coverage, length=length, count=count)
        edge_contrast = minimum_edge / max(0.02, interior_edge)
        coverage_contrast = minimum_coverage / max(0.08, interior_coverage)
        if hinted_count != count and max(edge_contrast, coverage_contrast) < MIN_BOUNDARY_CONTRAST:
            continue
        divisibility_bonus = 0.05 if length % count == 0 else 0.0
        hint_bonus = 0.08 if hinted_count == count else 0.0
        score = (
            minimum_coverage * 0.58
            + average_coverage * 0.22
            + min(1.0, minimum_edge / 0.20) * 0.15
            + divisibility_bonus
            + hint_bonus
            - max(0, count - 2) * 0.035
        )
        if score > best.score:
            best = AxisLayout(count=count, score=min(1.0, score))

    periodic = _detect_periodic_axis(edge)
    if periodic.score > best.score:
        best = periodic
    return best


def _background_axis_layout(image: Image.Image, *, axis: int) -> AxisLayout | None:
    """Infer repeated cells from full-transparent runs."""
    if "A" not in image.getbands():
        return None
    alpha = image.getchannel("A")
    if alpha.getextrema()[0] >= 255:
        return None

    pixels = alpha.load()
    width, height = image.size
    length = width if axis == 0 else height
    cross = height if axis == 0 else width

    background_coverage = [
        sum(_axis_pixel(pixels, axis, position, other) < 48 for other in range(cross)) / cross
        for position in range(length)
    ]
    runs = _true_runs([value >= 0.94 for value in background_coverage])
    if not runs or (len(runs) == 1 and runs[0] == (0, length)):
        return None
    start_run = runs[0] if runs[0][0] == 0 else None
    end_run = runs[-1] if runs[-1][1] == length else None
    inner_runs = [run for run in runs if run is not start_run and run is not end_run]
    if not 1 <= len(inner_runs) <= 7:
        return None

    gap_widths = [end - start for start, end in inner_runs]
    centers = [(start + end) / 2 for start, end in inner_runs]
    intervals = [right - left for left, right in zip(centers, centers[1:])]
    consistency = min(_consistency(gap_widths), _consistency(intervals))
    if consistency < 0.50:
        return None
    return AxisLayout(count=len(inner_runs) + 1, score=0.75 + consistency * 0.25)


def _true_runs(values: list[bool]) -> list[tuple[int, int]]:
    runs = []
    start = None
    for index, value in enumerate([*values, False]):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append((start, index))
            start = None
    return runs


def _has_sparse_transparency(image: Image.Image) -> bool:
    if "A" not in image.getbands() or image.getchannel("A").getextrema()[0] >= 255:
        return False
    pixels = image.load()
    width, height = image.size
    visible = sum(pixels[x, y][3] >= 32 for y in range(height) for x in range(width))
    return visible / max(1, width * height) < MIN_VISIBLE_RATIO_FOR_VISUAL_DETECTION


def _cell_interior_median(values: list[float], *, length: int, count: int) -> float:
    cell_width = length / count
    return median(
        _range_average(
            values,
            round(index * cell_width + cell_width * 0.20),
            round(index * cell_width + cell_width * 0.80),
        )
        for index in range(count)
    )


def _consistency(values: list[float]) -> float:
    if len(values) < 2:
        return 1.0
    average = sum(values) / len(values)
    spread = max(values) - min(values)
    return 1.0 - min(1.0, spread / max(average, 1.0))


def _detect_periodic_axis(profile: list[float]) -> AxisLayout:
    smoothed = _smooth_profile(profile, radius=2)
    length = len(smoothed)
    if length < 32:
        return AxisLayout(count=1, score=0.0)
    mean_activity = _range_average(smoothed, 2, length - 2)
    if mean_activity < MIN_PERIODIC_ACTIVITY:
        return AxisLayout(count=1, score=0.0)

    best = AxisLayout(count=1, score=0.0)
    for count in range(4, 9):
        if length < count * 8:
            continue
        edges = [round(length * index / count) for index in range(count + 1)]
        segment_activity = [
            _range_average(smoothed, edges[index], edges[index + 1]) for index in range(count)
        ]
        average_segment = sum(segment_activity) / len(segment_activity)
        uniformity = min(segment_activity) / max(average_segment, 1e-6)
        if uniformity < MIN_PERIODIC_UNIFORMITY:
            continue

        radius = max(1, round(length / count * 0.06))
        boundary_activity = [
            _range_average(smoothed, position - radius, position + radius + 1)
            for position in edges[1:-1]
        ]
        boundary_ratio = max(boundary_activity) / max(mean_activity, 1e-6)
        if boundary_ratio > MAX_PERIODIC_BOUNDARY_RATIO:
            continue

        valley_score = 1.0 - boundary_ratio / MAX_PERIODIC_BOUNDARY_RATIO
        score = valley_score * 0.65 + uniformity * 0.35
        if score > best.score:
            best = AxisLayout(count=count, score=score)
    return best if best.score >= MIN_PERIODIC_SCORE else AxisLayout(count=1, score=0.0)


def _axis_profiles(image: Image.Image, axis: int) -> tuple[list[float], list[float]]:
    pixels = image.load()
    width, height = image.size
    length = width if axis == 0 else height
    cross = height if axis == 0 else width
    edge = [0.0] * length
    coverage = [0.0] * length

    for position in range(1, length):
        edge_total = 0.0
        strong = 0
        soft = 0
        for other in range(cross):
            difference = _pixel_distance(
                _axis_pixel(pixels, axis, position, other),
                _axis_pixel(pixels, axis, position - 1, other),
            )
            edge_total += difference
            strong += difference >= 0.075
            soft += difference >= 0.025
        edge[position] = edge_total / cross
        coverage[position] = (strong * 0.75 + soft * 0.25) / cross
    return edge, coverage


def _axis_pixel(pixels, axis: int, position: int, other: int):
    return pixels[position, other] if axis == 0 else pixels[other, position]


def _pixel_distance(first: tuple[int, ...], second: tuple[int, ...]) -> float:
    if len(first) >= 4 and len(second) >= 4:
        first_alpha = first[3] / 255
        second_alpha = second[3] / 255
        first_rgb = [channel * first_alpha for channel in first[:3]]
        second_rgb = [channel * second_alpha for channel in second[:3]]
        color_distance = sum(abs(left - right) for left, right in zip(first_rgb, second_rgb))
        alpha_distance = abs(first[3] - second[3])
        return (color_distance + alpha_distance) / (4 * 255)
    return sum(abs(left - right) for left, right in zip(first, second)) / (len(first) * 255)


def _window_max(values: list[float], position: int, radius: int) -> float:
    start = max(0, position - radius)
    end = min(len(values), position + radius + 1)
    return max(values[start:end], default=0.0)


def _smooth_profile(values: list[float], *, radius: int) -> list[float]:
    return [
        _range_average(values, index - radius, index + radius + 1) for index in range(len(values))
    ]


def _range_average(values: list[float], start: int, end: int) -> float:
    if not values:
        return 0.0
    lower = max(0, min(len(values) - 1, start))
    upper = min(len(values), max(lower + 1, end))
    return sum(values[lower:upper]) / (upper - lower)


def _grid_hint(prompt: str) -> tuple[int, int] | None:
    normalized = prompt.lower().replace("＊", "×")
    match = re.search(r"([1-8])\s*行\s*([1-8])\s*列", normalized)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"(?<!\d)([1-8])\s*[x×*]\s*([1-8])(?!\d)", normalized)
    if match:
        return int(match.group(1)), int(match.group(2))
    if "九宫格" in normalized:
        return 3, 3
    if "四宫格" in normalized:
        return 2, 2
    return None
