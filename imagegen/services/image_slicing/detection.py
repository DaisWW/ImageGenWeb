from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageOps

from .geometry import grid_boxes
from .models import ANALYSIS_MAX_SIDE, AxisLayout

MIN_VISUAL_COVERAGE = 0.35
MIN_HINTED_COVERAGE = 0.18
PROMPT_FALLBACK_SCORE = 0.50
MIN_LAYOUT_SCORE = 0.45
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
    x_layout = _detect_axis(sample, axis=0, hinted_count=hint[1] if hint else None)
    y_layout = _detect_axis(sample, axis=1, hinted_count=hint[0] if hint else None)
    if hint:
        hinted_rows, hinted_columns = hint
        if x_layout.count == 1:
            x_layout = AxisLayout(count=hinted_columns, score=PROMPT_FALLBACK_SCORE)
        if y_layout.count == 1:
            y_layout = AxisLayout(count=hinted_rows, score=PROMPT_FALLBACK_SCORE)

    rows = y_layout.count
    columns = x_layout.count
    boxes = grid_boxes(width, height, rows=rows, columns=columns)
    active_scores = [
        layout.score for layout in (x_layout, y_layout) if layout.count > 1
    ]
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


def _detect_axis(image: Image.Image, *, axis: int, hinted_count: int | None) -> AxisLayout:
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
        required_coverage = (
            MIN_HINTED_COVERAGE if hinted_count == count else MIN_VISUAL_COVERAGE
        )
        if minimum_coverage < required_coverage:
            continue

        average_coverage = sum(boundary_coverage) / len(boundary_coverage)
        minimum_edge = min(boundary_edge)
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
    return best if best.score >= MIN_LAYOUT_SCORE else AxisLayout(count=1, score=0.0)


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
            _range_average(smoothed, edges[index], edges[index + 1])
            for index in range(count)
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
    return best


def _axis_profiles(image: Image.Image, axis: int) -> tuple[list[float], list[float]]:
    pixels = image.load()
    width, height = image.size
    length = width if axis == 0 else height
    cross = height if axis == 0 else width
    edge = [0.0] * length
    coverage = [0.0] * length

    def pixel(position: int, other: int) -> tuple[int, int, int, int]:
        return pixels[position, other] if axis == 0 else pixels[other, position]

    for position in range(1, length):
        edge_total = 0.0
        strong = 0
        soft = 0
        for other in range(cross):
            difference = _pixel_distance(pixel(position, other), pixel(position - 1, other))
            edge_total += difference
            strong += difference >= 0.075
            soft += difference >= 0.025
        edge[position] = edge_total / cross
        coverage[position] = (strong * 0.75 + soft * 0.25) / cross
    return edge, coverage


def _pixel_distance(first: tuple[int, ...], second: tuple[int, ...]) -> float:
    return sum(abs(left - right) for left, right in zip(first, second)) / (len(first) * 255)


def _window_max(values: list[float], position: int, radius: int) -> float:
    start = max(0, position - radius)
    end = min(len(values), position + radius + 1)
    return max(values[start:end], default=0.0)


def _smooth_profile(values: list[float], *, radius: int) -> list[float]:
    return [
        _range_average(values, index - radius, index + radius + 1)
        for index in range(len(values))
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
