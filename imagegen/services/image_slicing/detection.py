from __future__ import annotations

import math
import re
from pathlib import Path
from statistics import median

from PIL import Image, ImageOps

from .geometry import grid_boxes
from .models import ANALYSIS_MAX_SIDE, AxisLayout


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

    columns = x_layout.count if x_layout.score >= 0.42 else 1
    rows = y_layout.count if y_layout.score >= 0.42 else 1
    if hint:
        hinted_rows, hinted_columns = hint
        if y_layout.count == hinted_rows and y_layout.score >= 0.30:
            rows = hinted_rows
        if x_layout.count == hinted_columns and x_layout.score >= 0.30:
            columns = hinted_columns

    if rows * columns == 1:
        stronger = x_layout if x_layout.score >= y_layout.score else y_layout
        if stronger.score >= 0.30:
            if stronger is x_layout:
                columns = stronger.count
            else:
                rows = stronger.count

    margin_x = _scale(x_layout.margin, sample.width, width) if columns > 1 else 0
    margin_y = _scale(y_layout.margin, sample.height, height) if rows > 1 else 0
    gap_x = _scale(x_layout.gap, sample.width, width) if columns > 1 else 0
    gap_y = _scale(y_layout.gap, sample.height, height) if rows > 1 else 0
    boxes = grid_boxes(
        width,
        height,
        rows=rows,
        columns=columns,
        margin_x=margin_x,
        margin_y=margin_y,
        gap_x=gap_x,
        gap_y=gap_y,
    )

    active_scores = [
        layout.score for layout, count in ((x_layout, columns), (y_layout, rows)) if count > 1
    ]
    score = sum(active_scores) / len(active_scores) if active_scores else 0.0
    if rows > 1 and columns > 1:
        score = min(1.0, score + 0.05)
    detected = len(boxes) > 1 and score >= 0.48
    confidence = "high" if score >= 0.72 else "medium" if score >= 0.54 else "low"
    if not detected:
        confidence = "low"

    return {
        "width": width,
        "height": height,
        "detected": detected,
        "confidence": confidence,
        "rows": rows,
        "columns": columns,
        "margin_x": margin_x,
        "margin_y": margin_y,
        "gap_x": gap_x,
        "gap_y": gap_y,
        "boxes": [
            box.as_dict(row=index // columns, column=index % columns)
            for index, box in enumerate(boxes)
        ],
    }


def _detect_axis(image: Image.Image, *, axis: int, hinted_count: int | None) -> AxisLayout:
    background_layout = _background_axis_layout(image, axis=axis, hinted_count=hinted_count)
    if background_layout is not None:
        return background_layout

    edge, coverage, texture = _axis_profiles(image, axis)
    length = len(texture)
    edge_normalized = _normalize_high(edge)
    coverage_normalized = _normalize_high(coverage)
    flatness = _normalize_low(texture)
    seam = [
        edge_normalized[index] * 0.62 + coverage_normalized[index] * 0.38 for index in range(length)
    ]
    best = AxisLayout(count=2, margin=0, gap=0, score=0.0)
    max_margin = min(32, round(length * 0.14))
    max_gap = min(24, round(length * 0.10))

    for count in range(2, 9):
        for margin in range(max_margin + 1):
            for gap in range(max_gap + 1):
                usable = length - margin * 2 - gap * (count - 1)
                if usable < count * 8:
                    continue
                cell = usable / count
                boundaries = [
                    margin + cell * index + gap * (index - 1) for index in range(1, count)
                ]
                seam_scores = []
                boundary_coverages = []
                gutter_scores = []
                for start in boundaries:
                    if gap:
                        end = start + gap
                        seam_scores.append((_local_max(seam, start) + _local_max(seam, end)) / 2)
                        boundary_coverages.append(
                            max(_local_max(coverage, start), _local_max(coverage, end))
                        )
                        if gap >= 3:
                            gutter_scores.append(_range_average(flatness, start, end))
                    else:
                        seam_scores.append(_local_max(seam, start))
                        boundary_coverages.append(_local_max(coverage, start))
                minimum_coverage = min(boundary_coverages)
                required_coverage = 0.32 if hinted_count == count else 0.48
                if minimum_coverage < required_coverage:
                    continue
                average_seam = sum(seam_scores) / len(seam_scores)
                minimum_seam = min(seam_scores)
                gutter_score = sum(gutter_scores) / len(gutter_scores) if gutter_scores else 0.0
                margin_score = 0.0
                if margin >= 3:
                    margin_score = (
                        _range_average(flatness, 0, margin)
                        + _range_average(flatness, length - margin, length)
                    ) / 2
                support = max(gutter_score, margin_score)
                score = average_seam * 0.66 + minimum_seam * 0.24 + support * 0.10
                score += min(0.045, math.log2(count) * 0.015)
                score -= max(0, count - 2) * 0.025
                score -= min(0.08, (margin + gap) / length * 0.75)
                if gap or margin:
                    score -= 0.10
                if (gap or margin) and support < 0.18:
                    score -= 0.07
                if hinted_count == count:
                    score += 0.10
                if score > best.score:
                    best = AxisLayout(count=count, margin=margin, gap=gap, score=min(1.0, score))
    return best


def _background_axis_layout(
    image: Image.Image,
    *,
    axis: int,
    hinted_count: int | None,
) -> AxisLayout | None:
    pixels = image.load()
    width, height = image.size
    patch_width = max(1, min(8, round(width * 0.03)))
    patch_height = max(1, min(8, round(height * 0.03)))
    corner_pixels = []
    for left, top in (
        (0, 0),
        (width - patch_width, 0),
        (0, height - patch_height),
        (width - patch_width, height - patch_height),
    ):
        corner_pixels.extend(
            pixels[x, y]
            for y in range(top, top + patch_height)
            for x in range(left, left + patch_width)
        )
    background = tuple(int(median(channel)) for channel in zip(*corner_pixels))
    length = width if axis == 0 else height
    cross = height if axis == 0 else width
    coverage = []

    def pixel(position: int, other: int) -> tuple[int, int, int, int]:
        return pixels[position, other] if axis == 0 else pixels[other, position]

    for position in range(length):
        matching = sum(
            _pixel_distance(pixel(position, other), background) <= 0.045 for other in range(cross)
        )
        coverage.append(matching / cross)

    runs = _true_runs([value >= 0.94 for value in coverage])
    if not runs or (len(runs) == 1 and runs[0] == (0, length)):
        return None
    start_run = runs[0] if runs[0][0] == 0 else None
    end_run = runs[-1] if runs[-1][1] == length else None
    inner_runs = [
        run for run in runs if run is not start_run and run is not end_run and run[1] - run[0] >= 1
    ]
    if not 1 <= len(inner_runs) <= 7:
        return None
    count = len(inner_runs) + 1

    centers = [(start + end) / 2 for start, end in inner_runs]
    intervals = [right - left for left, right in zip(centers, centers[1:])]
    regularity = 1.0
    if intervals:
        average = sum(intervals) / len(intervals)
        regularity = 1.0 - min(1.0, max(abs(value - average) for value in intervals) / average)
    gap_widths = [end - start for start, end in inner_runs]
    gap = round(sum(gap_widths) / len(gap_widths))
    gap_consistency = 1.0 - min(
        1.0,
        max(abs(value - gap) for value in gap_widths) / max(1, gap),
    )
    margins = [run[1] - run[0] for run in (start_run, end_run) if run is not None]
    margin = round(sum(margins) / len(margins)) if margins else 0
    margin_consistency = 0.5
    if len(margins) == 2:
        margin_consistency = 1.0 - min(1.0, abs(margins[0] - margins[1]) / max(1, margin))
    score = regularity * 0.45 + gap_consistency * 0.30 + margin_consistency * 0.15 + 0.10
    if score < 0.72:
        return None
    return AxisLayout(count=count, margin=margin, gap=gap, score=min(1.0, score))


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


def _axis_profiles(image: Image.Image, axis: int) -> tuple[list[float], list[float], list[float]]:
    pixels = image.load()
    width, height = image.size
    length = width if axis == 0 else height
    cross = height if axis == 0 else width
    edge = [0.0] * length
    coverage = [0.0] * length
    texture = [0.0] * length

    def pixel(position: int, other: int) -> tuple[int, int, int, int]:
        return pixels[position, other] if axis == 0 else pixels[other, position]

    for position in range(length):
        texture_total = 0.0
        for other in range(1, cross):
            texture_total += _pixel_distance(pixel(position, other), pixel(position, other - 1))
        texture[position] = texture_total / max(1, cross - 1)
        if position == 0:
            continue
        edge_total = 0.0
        covered = 0
        for other in range(cross):
            difference = _pixel_distance(pixel(position, other), pixel(position - 1, other))
            edge_total += difference
            covered += difference >= 0.075
        edge[position] = edge_total / max(1, cross)
        coverage[position] = covered / max(1, cross)
    return edge, coverage, texture


def _pixel_distance(first: tuple[int, ...], second: tuple[int, ...]) -> float:
    return sum(abs(left - right) for left, right in zip(first, second)) / (len(first) * 255)


def _normalize_high(values: list[float]) -> list[float]:
    center = median(values)
    high = _percentile(values, 0.95)
    span = max(0.012, high - center)
    return [_clamp((value - center) / span) for value in values]


def _normalize_low(values: list[float]) -> list[float]:
    center = median(values)
    low = _percentile(values, 0.08)
    span = max(0.012, center - low)
    return [_clamp((center - value) / span) for value in values]


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def _local_max(values: list[float], position: float) -> float:
    center = round(position)
    start = max(0, center - 2)
    end = min(len(values), center + 3)
    return max(values[start:end], default=0.0)


def _range_average(values: list[float], start: float, end: float) -> float:
    lower = max(0, round(start))
    upper = min(len(values), max(lower + 1, round(end)))
    return sum(values[lower:upper]) / max(1, upper - lower)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _scale(value: int, sample_size: int, original_size: int) -> int:
    return round(value * original_size / sample_size)


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
