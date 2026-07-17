from __future__ import annotations

from dataclasses import dataclass

MAX_SLICES = 64
MIN_SLICE_SIDE = 4
ANALYSIS_MAX_SIDE = 384


@dataclass(frozen=True, slots=True)
class SliceBox:
    x: int
    y: int
    width: int
    height: int

    def as_dict(self, *, row: int, column: int) -> dict[str, int]:
        return {
            "row": row,
            "column": column,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class AxisLayout:
    count: int
    margin: int
    gap: int
    score: float
