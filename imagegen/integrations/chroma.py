from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Any, Mapping

from PIL import Image, ImageFilter

from ..errors import ServiceError
from .matting import MAX_MATTING_BYTES, _assert_safe_input

_NORMALIZATION = math.sqrt(3 * 255**2)
# Chroma keeps several full-resolution RGBA/alpha buffers in memory.
MAX_LOCAL_CHROMA_PIXELS = 16_000_000

PROFILE_DEFAULTS: dict[str, dict[str, float | int | bool]] = {
    "balanced": {
        "threshold": 0.18,
        "softness": 0.16,
        "despill_strength": 0.40,
        "edge_feather": 0,
        "preserve_alpha": True,
        "border_sample": 24,
    },
    "clean": {
        "threshold": 0.24,
        "softness": 0.10,
        "despill_strength": 0.65,
        "edge_feather": 0,
        "preserve_alpha": True,
        "border_sample": 24,
    },
    "soft": {
        "threshold": 0.12,
        "softness": 0.24,
        "despill_strength": 0.22,
        "edge_feather": 1,
        "preserve_alpha": True,
        "border_sample": 24,
    },
}
_MAX_BORDER_SAMPLES = 4096


@dataclass(frozen=True, slots=True)
class ChromaKeyConfig:
    """Validated, immutable settings for deterministic green-screen removal."""

    profile: str = "balanced"
    key_color: tuple[int, int, int] | None = None
    threshold: float = 0.18
    softness: float = 0.16
    despill_strength: float = 0.40
    edge_feather: int = 0
    preserve_alpha: bool = True
    border_sample: int = 24

    def __post_init__(self) -> None:
        profile = self.profile.strip().lower()
        if profile not in PROFILE_DEFAULTS:
            raise ValueError(f"不支持的 Chroma 配置档位：{self.profile}")
        object.__setattr__(self, "profile", profile)
        if self.key_color is not None:
            if len(self.key_color) != 3 or any(
                not isinstance(value, int) or not 0 <= value <= 255 for value in self.key_color
            ):
                raise ValueError("key_color 必须是三个 0 到 255 的整数")
        _validate_float(self.threshold, "threshold", 0.0, 1.0)
        _validate_float(self.softness, "softness", 0.001, 1.0)
        _validate_float(self.despill_strength, "despill_strength", 0.0, 1.0)
        if not isinstance(self.edge_feather, int) or not 0 <= self.edge_feather <= 8:
            raise ValueError("edge_feather 必须是 0 到 8 的整数")
        if not isinstance(self.preserve_alpha, bool):
            raise ValueError("preserve_alpha 必须是布尔值")
        if not isinstance(self.border_sample, int) or not 4 <= self.border_sample <= 128:
            raise ValueError("border_sample 必须是 4 到 128 的整数")

    @classmethod
    def from_mapping(
        cls,
        options: Mapping[str, Any] | None = None,
        *,
        model: str = "balanced",
    ) -> "ChromaKeyConfig":
        raw = dict(options or {})
        profile = str(raw.pop("profile", raw.pop("algorithm", model or "balanced"))).strip().lower()
        defaults = PROFILE_DEFAULTS.get(profile)
        if defaults is None:
            raise ValueError(f"不支持的 Chroma 配置档位：{profile}")
        values: dict[str, Any] = dict(defaults)
        aliases = {
            "similarity": "threshold",
            "threshold": "threshold",
            "softness": "softness",
            "despill": "despill_strength",
            "despill_strength": "despill_strength",
            "edge_feather": "edge_feather",
            "feather": "edge_feather",
            "preserve_alpha": "preserve_alpha",
            "border_sample": "border_sample",
        }
        for key, value in raw.items():
            target = aliases.get(str(key))
            if target is not None:
                values[target] = value
        return cls(
            profile=profile,
            key_color=_parse_key_color(raw.get("key_color")),
            threshold=_coerce_float(values["threshold"], "threshold"),
            softness=_coerce_float(values["softness"], "softness"),
            despill_strength=_coerce_float(values["despill_strength"], "despill_strength"),
            edge_feather=_coerce_int(values["edge_feather"], "edge_feather"),
            preserve_alpha=_coerce_bool(values["preserve_alpha"], "preserve_alpha"),
            border_sample=_coerce_int(values["border_sample"], "border_sample"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "key_color": list(self.key_color) if self.key_color is not None else None,
            "threshold": self.threshold,
            "softness": self.softness,
            "despill_strength": self.despill_strength,
            "edge_feather": self.edge_feather,
            "preserve_alpha": self.preserve_alpha,
            "border_sample": self.border_sample,
        }


class ChromaKeyAdapter:
    """CPU-only keyer for green-screen images with soft alpha edges."""

    adapter_id = "chroma_key"

    def __init__(
        self,
        config: ChromaKeyConfig | Mapping[str, Any] | None = None,
        *,
        model: str = "balanced",
    ) -> None:
        self.config = (
            config
            if isinstance(config, ChromaKeyConfig)
            else ChromaKeyConfig.from_mapping(config, model=model)
        )

    def healthcheck(self) -> None:
        return None

    def close(self) -> None:
        return None

    def remove_background(self, content: bytes, *, filename: str = "image.png") -> bytes:
        del filename
        if not content:
            raise ServiceError("图片内容为空", code="invalid_image")
        if len(content) > MAX_MATTING_BYTES:
            raise ServiceError(
                "图片超过 50 MiB 限制，无法进行本地透明化",
                code="matting_input_too_large",
            )
        _assert_safe_input(content, max_pixels=MAX_LOCAL_CHROMA_PIXELS)
        try:
            with Image.open(io.BytesIO(content)) as source:
                source.load()
                image = source.convert("RGBA")
        except ServiceError:
            raise
        except Exception as exc:  # Pillow normalizes decoder-specific failures.
            raise ServiceError("文件不是有效图片", code="invalid_image") from exc
        try:
            return _render_keyed_png(image, self.config)
        finally:
            image.close()


class TwoPassChromaKeyAdapter(ChromaKeyAdapter):
    """Combines a clean object pass with a softer particle-preserving pass."""

    adapter_id = "two_pass_chroma"

    def __init__(
        self,
        config: ChromaKeyConfig | Mapping[str, Any] | None = None,
        *,
        model: str = "balanced",
    ) -> None:
        if isinstance(config, ChromaKeyConfig):
            self.config = config
            self.object_config = config
            self.particle_config = ChromaKeyConfig.from_mapping({"profile": "soft"})
            return
        raw = dict(config or {})
        profile = str(raw.get("profile", model or "balanced")).strip().lower()
        base = ChromaKeyConfig.from_mapping(raw, model=profile)
        object_raw = dict(raw.get("object", {})) if isinstance(raw.get("object"), dict) else {}
        particle_raw = (
            dict(raw.get("particle", {})) if isinstance(raw.get("particle"), dict) else {}
        )
        object_raw.setdefault("profile", "clean")
        particle_raw.setdefault("profile", "soft")
        if base.key_color is not None:
            object_raw.setdefault("key_color", list(base.key_color))
            particle_raw.setdefault("key_color", list(base.key_color))
        self.config = base
        self.object_config = ChromaKeyConfig.from_mapping(object_raw, model="clean")
        self.particle_config = ChromaKeyConfig.from_mapping(particle_raw, model="soft")

    def remove_background(self, content: bytes, *, filename: str = "image.png") -> bytes:
        del filename
        if not content:
            raise ServiceError("图片内容为空", code="invalid_image")
        if len(content) > MAX_MATTING_BYTES:
            raise ServiceError(
                "图片超过 50 MiB 限制，无法进行本地透明化",
                code="matting_input_too_large",
            )
        _assert_safe_input(content, max_pixels=MAX_LOCAL_CHROMA_PIXELS)
        try:
            with Image.open(io.BytesIO(content)) as source:
                source.load()
                image = source.convert("RGBA")
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("文件不是有效图片", code="invalid_image") from exc
        try:
            return _render_two_pass_png(image, self.object_config, self.particle_config)
        finally:
            image.close()


# A descriptive alias for callers that prefer the strategy name.
HybridChromaKeyAdapter = TwoPassChromaKeyAdapter
TwoPassMattingAdapter = TwoPassChromaKeyAdapter


def _render_keyed_png(image: Image.Image, config: ChromaKeyConfig) -> bytes:
    rgba = image.tobytes()
    width, height = image.size
    key = config.key_color or _sample_border_color(rgba, width, height, config.border_sample)
    alpha = _compute_alpha(rgba, key, config)
    return _encode_foreground(rgba, alpha, key, width, height, config)


def _render_two_pass_png(
    image: Image.Image,
    object_config: ChromaKeyConfig,
    particle_config: ChromaKeyConfig,
) -> bytes:
    rgba = image.tobytes()
    width, height = image.size
    key = object_config.key_color or particle_config.key_color
    key = key or _sample_border_color(
        rgba,
        width,
        height,
        max(object_config.border_sample, particle_config.border_sample),
    )
    object_alpha = _compute_alpha(rgba, key, object_config)
    particle_alpha = _compute_alpha(rgba, key, particle_config)
    combined = bytearray(len(object_alpha))
    for index, (foreground, particle) in enumerate(zip(object_alpha, particle_alpha)):
        # Soft pass mainly fills semi-transparent light particles; solid object
        # pixels remain governed by the cleaner first pass.
        combined[index] = max(foreground, particle)
    return _encode_foreground(rgba, combined, key, width, height, object_config)


def _compute_alpha(
    rgba: bytes,
    key: tuple[int, int, int],
    config: ChromaKeyConfig,
) -> bytearray:
    alpha = bytearray(len(rgba) // 4)
    lower = config.threshold
    upper = min(1.0, lower + config.softness)
    span = max(upper - lower, 0.001)
    kr, kg, kb = key
    for pixel_index in range(len(alpha)):
        offset = pixel_index * 4
        r, g, b, source_alpha = rgba[offset : offset + 4]
        distance = math.sqrt((r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2) / _NORMALIZATION
        ratio = max(0.0, min(1.0, (distance - lower) / span))
        # Smoothstep avoids a visible hard contour around particles.
        ratio = ratio * ratio * (3.0 - 2.0 * ratio)
        if config.preserve_alpha:
            ratio *= source_alpha / 255.0
        alpha[pixel_index] = round(ratio * 255)
    return alpha


def _encode_foreground(
    rgba: bytes,
    alpha: bytearray,
    key: tuple[int, int, int],
    width: int,
    height: int,
    config: ChromaKeyConfig,
) -> bytes:
    if config.edge_feather:
        alpha = _feather_alpha(alpha, width, height, config.edge_feather)
    if alpha and min(alpha) == 255:
        raise ServiceError(
            "本地绿幕未识别到透明背景，请改用 AI 透明化模型",
            code="matting_opaque_result",
            status_code=502,
        )
    kr, kg, kb = key
    output = bytearray(len(rgba))
    for pixel_index in range(len(alpha)):
        source_offset = pixel_index * 4
        target_offset = source_offset
        r, g, b, _source_alpha = rgba[source_offset : source_offset + 4]
        opacity = alpha[pixel_index] / 255.0
        if opacity <= 0.0:
            output[target_offset : target_offset + 4] = b"\x00\x00\x00\x00"
            continue
        if opacity < 0.98:
            # Reconstruct the foreground against the sampled screen colour.
            r = _unmix_channel(r, kr, opacity)
            g = _unmix_channel(g, kg, opacity)
            b = _unmix_channel(b, kb, opacity)
        spill = max(0, g - max(r, b))
        g = max(0, min(255, round(g - spill * config.despill_strength * (1.0 - opacity))))
        output[target_offset : target_offset + 4] = bytes(
            (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)), alpha[pixel_index])
        )
    result = Image.frombytes("RGBA", (width, height), bytes(output))
    try:
        stream = io.BytesIO()
        result.save(stream, format="PNG", compress_level=6, optimize=False)
        encoded = stream.getvalue()
        if len(encoded) > MAX_MATTING_BYTES:
            raise ServiceError(
                "本地透明化结果超过 50 MiB 限制",
                code="matting_output_too_large",
                status_code=502,
            )
        return encoded
    finally:
        result.close()


def _feather_alpha(alpha: bytearray, width: int, height: int, radius: int) -> bytearray:
    mask = Image.frombytes("L", (width, height), bytes(alpha))
    blurred = None
    try:
        blurred = mask.filter(ImageFilter.GaussianBlur(radius=float(radius)))
        return bytearray(blurred.tobytes())
    finally:
        mask.close()
        if blurred is not None:
            blurred.close()


def _sample_border_color(
    rgba: bytes, width: int, height: int, sample_side: int
) -> tuple[int, int, int]:
    side = min(sample_side, width, height)
    stride = max(1, max(width, height) // 256)
    horizontal_bands = [(0, side)]
    if height > side:
        horizontal_bands.append((height - side, height))
    vertical_bands = [(0, side)]
    if width > side:
        vertical_bands.append((width - side, width))

    def axis_count(start: int, end: int) -> int:
        if end <= start:
            return 0
        first = start + ((-start) % stride)
        if first >= end:
            first = start
        count = ((end - 1 - first) // stride) + 1
        if (end - 1 - first) % stride:
            count += 1
        return count

    def estimated_count() -> int:
        horizontal = sum(axis_count(y0, y1) for y0, y1 in horizontal_bands) * axis_count(0, width)
        vertical = sum(axis_count(x0, x1) for x0, x1 in vertical_bands) * axis_count(0, height)
        return horizontal + vertical

    while estimated_count() > _MAX_BORDER_SAMPLES:
        stride *= 2

    def positions(start: int, end: int):
        if end <= start:
            return
        first = start + ((-start) % stride)
        if first >= end:
            first = start
        last = None
        for value in range(first, end, stride):
            last = value
            yield value
        if last != end - 1:
            yield end - 1

    samples: list[tuple[int, int, int]] = []
    for y0, y1 in horizontal_bands:
        for y in positions(y0, y1):
            for x in positions(0, width):
                offset = (y * width + x) * 4
                r, g, b, a = rgba[offset : offset + 4]
                if a >= 16:
                    samples.append((r, g, b))
    for x0, x1 in vertical_bands:
        for x in positions(x0, x1):
            for y in positions(0, height):
                offset = (y * width + x) * 4
                r, g, b, a = rgba[offset : offset + 4]
                if a >= 16:
                    samples.append((r, g, b))

    if not samples:
        return (0, 255, 0)
    green_samples = [sample for sample in samples if sample[1] >= max(sample[0], sample[2]) + 15]
    selected = green_samples if len(green_samples) >= max(8, len(samples) // 5) else samples
    return tuple(
        sorted(sample[index] for sample in selected)[len(selected) // 2] for index in range(3)
    )  # type: ignore[return-value]


def _unmix_channel(value: int, background: int, opacity: float) -> int:
    if opacity >= 0.98:
        return value
    return round(max(0.0, min(255.0, (value - background * (1.0 - opacity)) / opacity)))


def _parse_key_color(value: Any) -> tuple[int, int, int] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) != 6:
            raise ValueError("key_color 必须是 #RRGGBB 或三个整数")
        try:
            return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
        except ValueError as exc:
            raise ValueError("key_color 必须是 #RRGGBB 或三个整数") from exc
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return tuple(int(item) for item in value)  # type: ignore[return-value]
        except (TypeError, ValueError) as exc:
            raise ValueError("key_color 必须是 #RRGGBB 或三个整数") from exc
    raise ValueError("key_color 必须是 #RRGGBB 或三个整数")


def _validate_float(value: float, name: str, minimum: float, maximum: float) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} 必须是有限数字")
    if not minimum <= float(value) <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")


def _coerce_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc


def _coerce_int(value: Any, name: str) -> int:
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if isinstance(value, float) and value != converted:
        raise ValueError(f"{name} 必须是整数")
    return converted


def _coerce_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是布尔值")
