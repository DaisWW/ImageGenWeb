from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ServiceError
from .common import parse_json_object
from .creative import (
    PROMPT_CRAFT_GUIDANCE,
    SOURCE_METADATA,
    catalog_tag_labels,
    creative_direction_prompt,
    get_creative_direction,
    get_prompt_template,
    normalize_catalog_tags,
    normalize_template_id,
)


@dataclass(frozen=True, slots=True)
class PromptDraftReview:
    translate_to_english: bool
    workspace_kind: str
    workspace_prompt: str
    conversation_prompt: str = ""
    runtime_prompt: str = ""
    generation_prompt: str = ""
    creative_direction_id: str = "auto"
    max_prompt_characters: int = 8000

    def system_prompt(self) -> str:
        target = (
            "prompt 必须是自然、具体、结构清晰的英文生图提示词"
            if self.translate_to_english
            else "prompt 必须是自然、具体、结构清晰的中文生图提示词"
        )
        task = "帧动画" if self.workspace_kind == "animation" else "静态图片"
        runtime_section = (
            f"\n本次任务的运行参数如下：\n{self.runtime_prompt.strip()}"
            if self.runtime_prompt.strip()
            else ""
        )
        generation_section = (
            f"\n{self.generation_prompt.strip()}" if self.generation_prompt.strip() else ""
        )
        conversation_section = (
            f"\n对话行为规则如下：\n{self.conversation_prompt.strip()}"
            if self.conversation_prompt.strip()
            else ""
        )
        direction_section = creative_direction_prompt(self.creative_direction_id)
        return f"""你是高级 AI 视觉创作搭档与提示词工程师。本次调用同时完成需求确认和最终提示词整理：先判断会话是否已经足够明确，再决定是继续澄清还是直接为 GPT Image 2 整理最终提示词。
独立核对用户已经确认的事实、用户明确授权 AI 决定的事项、未解决问题和互相冲突的要求。助手曾提出但用户没有接受的建议不能视为已确认；用户明确回答“你决定”或同义表达时，该项视为已授权，不要再次阻塞。
只有缺失或冲突会让主体、用途、构图、风格、精确文字、参考图用途或动画动作产生明显不同结果时，才判定需要澄清。不要为了补齐所有常见参数而阻塞；不影响核心意图的衔接细节可采用克制、专业且不抢戏的默认选择。
若需要澄清，先完整核对会话，筛掉不会明显改变结果的低影响细节，只保留信息增益最高、互不重复且容易回答的阻塞性问题。把当前能够识别的问题一次性输出，问题宁少勿多，最多四个；不得把已经能识别的问题拆到后续轮次，也不要为了凑满四个补充问题。只有用户回答后新出现、且此前无法判断的关键分支或冲突，才允许追加追问。
questions 数组的每一项只放一个问题。适合枚举时，在同一字符串内换行列出“A.、B.、C.、D.……”选项，标明一个“（推荐）”，并把最后一项写为“其他（请自定义）”；无法合理枚举时直接要求填写具体内容。用户也可以自由输入或回答“你决定”；此时禁止输出半成品提示词。
若需求已足够明确，{target}，准确描述主体、动作、环境、构图、镜头、光线、材质、色彩和风格，不要堆砌互相冲突的关键词。summary_zh 要让用户能够核对所有关键事实、授权决定与精确限制。
当前任务是{task}。请遵循以下工作站创作指导：
{self.workspace_prompt.strip()}{conversation_section}{runtime_section}{generation_section}

{direction_section}

{PROMPT_CRAFT_GUIDANCE.strip()}

ready 时还必须完成一次交付前审查：
- 若本次输入包含候选图片，必须结合用户语义决定 reference_usage：generation 表示图片必须作为最终生图输入，analysis_only 表示图片只用于分析理解；没有候选图片时使用 none。reference_reason 用一句中文解释判断依据。不得仅因上传了图片就机械选择 generation，也不得在用户要求仿照、延续、修改或保持图片特征时静默丢图。
- creative_direction 必须是给定目录中的一个 ID；用户锁定方向时不得改选。
- template_id 必须是目录中的一个模板 ID；确实没有近似模板时使用 custom。style_tags、scene_tags 必须从目录值中选择，selection_reason 用一句中文解释匹配依据。
- brief 必须把模糊会话压缩成交付物、用途、主体、构图、风格、精确文字、参考图计划、保持项、改变项和禁止项。没有的内容使用空字符串或空数组，不得臆造。
- hard_checks 只列能从最终图片判断的 2～6 个硬门槛，例如精确文字、主体数量、必要元素、参考图身份、非目标区域保持和禁止额外内容。
- quality_hint 只能是 low、medium 或 high；它表示当前提示词首次试生成的建议，最终成品可由用户切换 high。
只输出一个 JSON 对象，不要 Markdown，不要额外说明，并严格使用以下两种格式之一：
{{"status":"needs_clarification","questions":["问题 1","问题 2"],"creative_direction":"poster"}}
{{"status":"ready","summary_zh":"中文需求确认","prompt":"最终生图提示词","reference_usage":"generation","reference_reason":"用户要求保持参考图主体并修改背景。","creative_direction":"poster","template_id":"poster-layout-system","style_tags":["Poster"],"scene_tags":["Commerce"],"selection_reason":"交付物是商业海报，需明确版式与文字层级。","brief":{{"deliverable":"交付物","intended_use":"用途与受众","subject":"主体","composition":"构图与画幅","style":"媒介、材质、光线与配色","exact_text":["必须逐字出现的文字"],"reference_plan":[{{"image_number":1,"role":"职责","preserve":["保持项"],"change":["改变项"]}}],"preserve":["全局保持项"],"change":["全局改变项"],"avoid":["禁止项"]}},"hard_checks":["可从成品判断的硬门槛"],"quality_hint":"low"}}"""

    def parse(self, content: str) -> dict[str, Any]:
        payload = parse_json_object(content)
        if payload is not None:
            parsed = self._clarification(payload) or self._ready(payload)
            if parsed is not None:
                return parsed
        raise ServiceError("聊天模型未能返回有效提示词草稿，请重试")

    def finalize(
        self,
        draft: dict[str, Any],
        *,
        generation_mode: str,
        reference_ids: list[str],
    ) -> dict[str, Any]:
        result = dict(draft)
        candidate_ids = list(reference_ids)
        usage = str(result.get("reference_usage", "")).strip().lower()
        if generation_mode == "auto":
            use_references = bool(candidate_ids) and usage not in {"analysis_only", "none"}
            generation_mode = "img2img" if use_references else "text2img"
            reference_ids = candidate_ids if use_references else []
        elif generation_mode == "img2img":
            reference_ids = candidate_ids
        else:
            reference_ids = []
        if generation_mode == "img2img" and not reference_ids:
            result.update(
                {
                    "status": "needs_clarification",
                    "questions": ["当前目标是参考图生图，请先上传或选择至少一张参考图。"],
                    "language": "en" if self.translate_to_english else "zh",
                }
            )
            for key in ("summary_zh", "prompt", "brief", "hard_checks", "quality_hint"):
                result.pop(key, None)
        result["reference_usage"] = (
            "generation" if reference_ids else "analysis_only" if candidate_ids else "none"
        )
        result["generation_mode"] = generation_mode
        result["reference_ids"] = reference_ids
        return result

    def message_content(self, draft: dict[str, Any]) -> tuple[str, str]:
        if draft["status"] == "needs_clarification":
            questions = "\n".join(
                f"{index}. {question}" for index, question in enumerate(draft["questions"], 1)
            )
            return f"为了让生成结果更符合预期，还需要确认：\n{questions}", "message"
        label = (
            "English prompt"
            if self.translate_to_english
            else ("帧动画提示词" if self.workspace_kind == "animation" else "生图提示词")
        )
        return f"需求确认\n{draft['summary_zh']}\n\n{label}\n{draft['prompt']}", "prompt_draft"

    def _clarification(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if str(payload.get("status", "")).strip().lower() != "needs_clarification":
            return None
        raw_questions = payload.get("questions")
        if not isinstance(raw_questions, list):
            return None
        questions = _string_list(raw_questions, 4, 500)
        if not questions:
            return None
        return {
            "status": "needs_clarification",
            "questions": questions,
            "language": "en" if self.translate_to_english else "zh",
            "creative_direction": _direction_id(
                payload.get("creative_direction"), self.creative_direction_id
            ),
            "reference_usage": _reference_usage(payload.get("reference_usage")),
            "reference_reason": str(payload.get("reference_reason", "")).strip()[:500],
            "sources": [dict(source) for source in SOURCE_METADATA],
        }

    def _ready(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if str(payload.get("status", "")).strip().lower() != "ready":
            return None
        summary = str(payload.get("summary_zh", "")).strip()
        prompt = str(payload.get("prompt", "")).strip()
        if not summary or not prompt:
            return None
        direction_id = _direction_id(payload.get("creative_direction"), self.creative_direction_id)
        return {
            "status": "ready",
            "summary_zh": summary[: self.max_prompt_characters],
            "prompt": prompt[: self.max_prompt_characters],
            "language": "en" if self.translate_to_english else "zh",
            "creative_direction": direction_id,
            "reference_usage": _reference_usage(payload.get("reference_usage")),
            "reference_reason": str(payload.get("reference_reason", "")).strip()[:500],
            **_catalog_selection(payload, direction_id),
            "brief": _brief(payload.get("brief")),
            "hard_checks": _string_list(payload.get("hard_checks"), 6, 300),
            "quality_hint": _quality_hint(payload.get("quality_hint")),
            "sources": [dict(source) for source in SOURCE_METADATA],
        }


def _direction_id(value: Any, fallback: str) -> str:
    selected = str(fallback or "auto").strip().lower()
    if selected != "auto":
        try:
            get_creative_direction(selected)
            return selected
        except ValueError:
            pass
    candidate = str(value or fallback or "auto").strip().lower()
    if candidate == "auto":
        return "other"
    try:
        get_creative_direction(candidate)
    except ValueError:
        return "other"
    return candidate


def _catalog_selection(payload: dict[str, Any], direction_id: str) -> dict[str, Any]:
    template_id = normalize_template_id(payload.get("template_id"), direction_id)
    template = get_prompt_template(template_id)
    style_tags = normalize_catalog_tags(payload.get("style_tags"))
    scene_tags = normalize_catalog_tags(payload.get("scene_tags"), scene=True)
    if template is not None:
        if not style_tags:
            style_tags = list(template.styles)
        if not scene_tags:
            scene_tags = list(template.scenes)
    return {
        "template_id": template_id,
        "template_label": template.label if template else "自定义 Craft",
        "style_tags": style_tags,
        "style_labels": catalog_tag_labels(style_tags),
        "scene_tags": scene_tags,
        "scene_labels": catalog_tag_labels(scene_tags, scene=True),
        "selection_reason": str(payload.get("selection_reason", "")).strip()[:500],
    }


def _quality_hint(value: Any) -> str:
    normalized = str(value or "low").strip().lower()
    return normalized if normalized in {"low", "medium", "high"} else "low"


def _reference_usage(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"generation", "analysis_only", "none"} else ""


def _string_list(value: Any, limit: int, maximum: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:maximum]
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _brief(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    brief = {
        key: str(raw.get(key, "")).strip()[:500]
        for key in ("deliverable", "intended_use", "subject", "composition", "style")
    }
    for key in ("exact_text", "preserve", "change", "avoid"):
        brief[key] = _string_list(raw.get(key), 12, 500)
    reference_plan = []
    raw_plan = raw.get("reference_plan")
    if isinstance(raw_plan, list):
        for item in raw_plan[:8]:
            if not isinstance(item, dict):
                continue
            try:
                image_number = max(1, min(8, int(item.get("image_number", 1))))
            except (TypeError, ValueError):
                image_number = 1
            reference_plan.append(
                {
                    "image_number": image_number,
                    "role": str(item.get("role", "")).strip()[:300],
                    "preserve": _string_list(item.get("preserve"), 8, 300),
                    "change": _string_list(item.get("change"), 8, 300),
                }
            )
    brief["reference_plan"] = reference_plan
    return brief
