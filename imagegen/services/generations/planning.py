from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ...errors import ServiceError
from ..series import SeriesAnchor

GENERATION_STRATEGIES = {"sample", "explore", "series"}
MAX_EXPLORATION_IMAGES = 4


@dataclass(frozen=True, slots=True)
class GenerationPlan:
    strategy: str
    prompts: tuple[str, ...]
    metadata: dict[str, object]

    @classmethod
    def build(
        cls,
        *,
        strategy: str,
        prompt: str,
        count: int,
        draft: dict[str, Any] | None,
        series_anchor: SeriesAnchor | dict[str, Any] | None,
        max_prompt_characters: int,
    ) -> GenerationPlan:
        normalized = normalize_generation_strategy(strategy)
        base_prompt = str(prompt).strip()
        metadata: dict[str, object] = {"generation_strategy": normalized}
        if normalized == "sample":
            prompts = tuple(base_prompt for _ in range(count))
        elif normalized == "explore":
            if draft is None:
                raise ServiceError(
                    "探索方案必须使用 AI 整理后的最终提示词",
                    code="prompt_review_required",
                    status_code=409,
                )
            if not 2 <= count <= MAX_EXPLORATION_IMAGES:
                raise ServiceError("探索方案每次必须生成 2 到 4 张图片")
            variants = _exploration_variants(draft.get("exploration_plan"), count)
            prompts = tuple(
                _append_contract(
                    base_prompt,
                    _exploration_contract(variant, language=str(draft.get("language", "zh"))),
                    max_prompt_characters,
                )
                for variant in variants
            )
            metadata["variant_plan"] = variants
        else:
            if draft is None:
                raise ServiceError(
                    "系列延续必须使用 AI 整理后的最终提示词",
                    code="prompt_review_required",
                    status_code=409,
                )
            anchor = SeriesAnchor.require(
                series_anchor,
                invalid_message="系列基准已失效，请重新选择",
                invalid_code="invalid_request",
            )
            prompts = tuple(
                _append_contract(
                    base_prompt,
                    _series_contract(anchor, language=str(draft.get("language", "zh"))),
                    max_prompt_characters,
                )
                for _ in range(count)
            )
            metadata["series_anchor"] = anchor.metadata()
            metadata["series_contract"] = anchor.contract
        return cls(strategy=normalized, prompts=prompts, metadata=metadata)


def normalize_generation_strategy(value: object) -> str:
    strategy = str(value or "sample").strip().lower()
    if strategy not in GENERATION_STRATEGIES:
        raise ServiceError("生成方式无效")
    return strategy


def _exploration_variants(value: object, count: int) -> list[dict[str, object]]:
    variants: list[dict[str, object]] = []
    if isinstance(value, list):
        for item in value[:MAX_EXPLORATION_IMAGES]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()[:80]
            raw_delta = item.get("delta")
            delta = (
                [str(entry).strip()[:300] for entry in raw_delta[:4] if str(entry).strip()]
                if isinstance(raw_delta, list)
                else []
            )
            if label and delta:
                variants.append({"label": label, "delta": delta})
            if len(variants) >= count:
                break
    if len(variants) < count:
        raise ServiceError("最终提示词没有足够的探索方案，请让 AI 重新整理需求")
    return variants


def _exploration_contract(variant: dict[str, object], *, language: str) -> str:
    payload = json.dumps(variant, ensure_ascii=False, indent=2)
    if language == "en":
        return (
            "Controlled exploration variant (only the declared dimensions may differ):\n"
            f"{payload}\n"
            "Keep every other part of the base prompt, subject identity, product geometry, exact "
            "text, reference-image roles, canvas, template, and hard requirement unchanged. "
            "Do not introduce any undeclared variation."
        )
    return (
        "受控探索方案（只能改变下列已声明维度）：\n"
        f"{payload}\n"
        "其余基础提示词、主体身份、产品外形、精确文字、参考图职责、画幅、模板和硬门槛"
        "必须保持一致；禁止引入未声明的变化。"
    )


def _series_contract(anchor: SeriesAnchor, *, language: str) -> str:
    payload = json.dumps(anchor.contract, ensure_ascii=False, indent=2)
    if language == "en":
        return (
            "Series continuity contract (must be repeated in this image):\n"
            f"{payload}\n"
            "Treat the selected reference image as the series anchor. Change only what the current "
            "request and allowed_changes explicitly permit; preserve all identity, visual-language, "
            "palette, material, composition, typography, and must_preserve rules."
        )
    return (
        "系列一致性契约（本张图片必须继续执行）：\n"
        f"{payload}\n"
        "所选参考图是系列基准。只改变当前需求和 allowed_changes 明确允许的内容；身份、视觉语言、"
        "色板、材质、构图、排版和 must_preserve 必须延续。"
    )


def _append_contract(prompt: str, contract: str, maximum: int) -> str:
    result = f"{prompt.rstrip()}\n\n{contract}"
    if len(result) > maximum:
        raise ServiceError(f"方案契约加入后提示词超过 {maximum} 个字符，请精简需求")
    return result
