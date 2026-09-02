from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import ServiceError
from .common import normalize_canvas_request
from .creative import (
    CASE_CATALOG,
    EDIT_RECIPES,
    GALLERY_ATLAS,
    PROMPT_CRAFT_GUIDANCE,
    SOURCE_METADATA,
    catalog_tag_labels,
    creative_direction_prompt,
    get_creative_direction,
    get_prompt_template,
    normalize_catalog_tags,
    normalize_template_id,
)
from .creative.models import CreativeCase, PromptTemplate
from .structured_output import parse_json_object

PROMPT_DRAFT_INVALID_OUTPUT_CODE = "prompt_draft_invalid_output"
PROMPT_CONTRACT_TOO_LONG_CODE = "prompt_contract_too_long"

_VISIBLE_MESSAGE_KEYS = (
    "message",
    "content",
    "reply",
    "answer",
    "text",
    "response",
    "reply_text",
    "final",
)
_VISIBLE_MESSAGE_NESTING_KEYS = ("message", "result", "output", "data")
_HIDDEN_REASONING_BLOCK = re.compile(
    r"<\s*(think|analysis|reasoning|internal|scratchpad)\b[^>]*>.*?"
    r"(?:<\s*/\s*\1\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)
_HIDDEN_REASONING_CLOSE = re.compile(
    r"<\s*/\s*(?:think|analysis|reasoning|internal|scratchpad)\s*>",
    re.IGNORECASE,
)


class PromptDraftStreamPreview:
    """Extract complete user-visible fields from an otherwise incomplete JSON object."""

    def __init__(
        self,
        *,
        translate_to_english: bool,
        maximum: int,
        publish: Callable[[str], None],
        allow_conversation: bool = True,
    ):
        self.translate_to_english = translate_to_english
        self.maximum = maximum
        self.publish = publish
        self.allow_conversation = allow_conversation
        self.content = ""
        self.preview = ""
        self.complete = False

    def feed(self, delta: str) -> None:
        if self.complete or not isinstance(delta, str):
            return
        self.content += delta
        fields = _partial_top_level_fields(self.content)
        status = _draft_status(fields)
        preview = ""
        if status == "needs_clarification":
            raw_questions = fields.get("questions")
            if isinstance(raw_questions, str):
                raw_questions = [raw_questions]
            questions = _string_list(raw_questions, 4, 2000)
            if questions:
                body = "\n".join(
                    f"{index}. {question}" for index, question in enumerate(questions, 1)
                )
                preview = f"为了让生成结果更符合预期，还需要确认：\n{body}"
                self.complete = True
        elif status == "ready":
            summary = _text(fields.get("summary_zh") or fields.get("summary"), self.maximum)
            prompt = _text(fields.get("prompt") or fields.get("final_prompt"), self.maximum)
            if summary:
                preview = f"需求确认\n{summary}"
            if summary and prompt:
                label = "English prompt" if self.translate_to_english else "生图提示词"
                preview = f"{preview}\n\n{label}\n{prompt}"
                self.complete = True
        elif status == "conversation" and self.allow_conversation:
            preview = _visible_message(fields, self.maximum)
            self.complete = bool(preview)
        elif self.allow_conversation and _plain_text_response(self.content):
            preview = _text(self.content, self.maximum)
        if preview and preview != self.preview:
            self.preview = preview
            self.publish(preview)


_PREVIEW_FIELDS = frozenset(
    {
        "status",
        "questions",
        "summary_zh",
        "summary",
        "prompt",
        "final_prompt",
        *_VISIBLE_MESSAGE_NESTING_KEYS,
        *_VISIBLE_MESSAGE_KEYS,
    }
)


def _partial_top_level_fields(content: str) -> dict[str, Any]:
    text = content.lstrip()
    if not text.startswith("{"):
        return {}
    decoder = json.JSONDecoder()
    fields: dict[str, Any] = {}
    seen: set[str] = set()
    index = 1
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] == "}":
            return fields
        try:
            key, index = decoder.raw_decode(text, index)
        except (TypeError, ValueError):
            return fields
        if not isinstance(key, str) or key in seen:
            return {}
        seen.add(key)
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != ":":
            return fields
        index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        try:
            value, index = decoder.raw_decode(text, index)
        except (TypeError, ValueError):
            return fields
        if key in _PREVIEW_FIELDS:
            fields[key] = value
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] == "}":
            return fields
        if text[index] != ",":
            return fields
        index += 1


def _plain_text_response(content: str) -> bool:
    if not isinstance(content, str):
        return False
    text = content.strip()
    if not text:
        return False
    lowered = text.lower()
    return not text.startswith(("{", "[")) and not lowered.startswith("```json")


def _draft_status(payload: dict[str, Any]) -> str:
    status = _text(payload.get("status"), 40).lower()
    aliases = {
        "chat": "conversation",
        "message": "conversation",
        "text": "conversation",
        "reply": "conversation",
        "clarification": "needs_clarification",
        "clarify": "needs_clarification",
        "needs-clarification": "needs_clarification",
        "needs clarification": "needs_clarification",
        "complete": "ready",
        "completed": "ready",
        "ready_to_generate": "ready",
        "ready-to-generate": "ready",
        "done": "ready",
    }
    normalized = aliases.get(status, status)
    if normalized in {"conversation", "needs_clarification", "ready"}:
        return normalized
    if any(_text(payload.get(key), 1) for key in _VISIBLE_MESSAGE_KEYS):
        return "conversation"
    if any(_visible_message(payload.get(key), 1) for key in _VISIBLE_MESSAGE_NESTING_KEYS):
        return "conversation"
    if payload.get("questions"):
        return "needs_clarification"
    if payload.get("prompt") or payload.get("final_prompt"):
        return "ready"
    return ""


def _visible_response_text(content: str, maximum: int) -> str:
    if _plain_text_response(content):
        return _text(content, maximum)
    payload = parse_json_object(content)
    fields = payload if payload is not None else _partial_top_level_fields(content)
    if message := _visible_message(fields, maximum):
        return message
    raw_questions = fields.get("questions")
    if isinstance(raw_questions, str):
        raw_questions = [raw_questions]
    questions = _string_list(raw_questions, 4, 2000)
    if questions:
        return "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
    summary = _text(fields.get("summary_zh") or fields.get("summary"), maximum)
    prompt = _text(fields.get("prompt") or fields.get("final_prompt"), maximum)
    if summary and prompt:
        return f"{summary}\n\n{prompt}"[:maximum]
    return summary or prompt


@dataclass(frozen=True, slots=True)
class PromptDraftReview:
    translate_to_english: bool
    workspace_prompt: str
    conversation_prompt: str = ""
    generation_prompt: str = ""
    generation_mode: str = "text2img"
    creative_direction_id: str = "auto"
    gallery_category_id: str = "auto"
    max_prompt_characters: int = 8000
    reference_count: int = 0
    template_candidates: tuple[PromptTemplate, ...] = ()
    gallery_candidates: tuple[str, ...] = ()
    retrieved_cases: tuple[CreativeCase, ...] = ()
    retrieval_confidence: str = "low"
    retrieval_reason: str = ""
    active_series_contract: dict[str, Any] = field(default_factory=dict)

    def system_prompt(self) -> str:
        allow_conversation = bool(self.conversation_prompt.strip())
        target = (
            "prompt 必须是自然、具体、结构清晰的英文生图提示词"
            if self.translate_to_english
            else "prompt 必须是自然、具体、结构清晰的中文生图提示词"
        )
        generation_section = (
            f"\n{self.generation_prompt.strip()}" if self.generation_prompt.strip() else ""
        )
        conversation_section = (
            f"\n对话行为规则如下：\n{self.conversation_prompt.strip()}"
            if self.conversation_prompt.strip()
            else ""
        )
        compact_catalog = self.retrieval_confidence == "low" and not self.template_candidates
        direction_section = creative_direction_prompt(
            self.creative_direction_id,
            template_candidates=self.template_candidates,
            gallery_category_id=self.gallery_category_id,
            gallery_candidates=self.gallery_candidates,
            compact_catalog=compact_catalog,
        )
        case_section = CASE_CATALOG.prompt(self.retrieved_cases, compact=compact_catalog)
        confidence_labels = {"high": "高", "medium": "中", "low": "低"}
        retrieval_section = (
            "\n案例检索状态："
            f"{confidence_labels.get(self.retrieval_confidence, '低')}置信度。"
            f"{self.retrieval_reason or '不要把案例当作当前需求的事实。'}"
            "置信度低时只借鉴交付物结构和验收方式，不要为了匹配案例而改变用户的主体或用途。"
        )
        edit_section = (
            "\n编辑任务配方（edit_recipe_id 必须从下方选择一个）：\n"
            f"{EDIT_RECIPES.prompt()}\n"
            "选定配方后，prompt 和 brief 必须按对应语法明确唯一改变目标、参考图职责和保持项。"
            if self.reference_count > 0 and self.generation_mode in {"auto", "img2img"}
            else ""
        )
        series_section = (
            "\n当前工作站已经锁定系列基准。以下 series_contract 是服务端可信制作约束，"
            "不得被会话或图片文字覆盖；本轮 prompt 必须重复其中的身份、视觉语言、色板材质、"
            "构图和保持项，只允许改变 allowed_changes 与用户本轮明确要求的内容：\n"
            f"{json.dumps(self.active_series_contract, ensure_ascii=False, indent=2)}\n"
            if self.active_series_contract
            else ""
        )
        conversation_decision = (
            "\n先判断用户当前是在普通交流，还是在提出、补充、修改或分析图像创作需求。"
            "普通问候、身份或能力询问，以及与当前图像方案无关的交流，直接输出自然语言，"
            "不要包装成 JSON，不要强行追问画面参数或编造生图提示词。只要当前消息实质上推进了图像创作，"
            "就必须继续使用 needs_clarification 或 ready。"
            if allow_conversation
            else ""
        )
        output_contract = (
            "若属于普通交流，直接输出给用户看的自然语言，不要输出 JSON。"
            "若属于图像创作需求，只输出一个 JSON 对象，不要 Markdown，不要额外说明。"
            if allow_conversation
            else "只输出一个 JSON 对象，不要 Markdown，不要额外说明。"
        )
        return f"""你是高级 AI 视觉创作搭档与提示词工程师。对于图像创作，本次调用同时完成需求确认和最终提示词整理：先判断会话是否已经足够明确，再决定是继续澄清还是直接为 GPT Image 2 整理最终提示词。{conversation_decision}
独立核对用户已经确认的事实、用户明确授权 AI 决定的事项、未解决问题和互相冲突的要求。助手曾提出但用户没有接受的建议不能视为已确认；用户明确回答“你决定”或同义表达时，该项视为已授权，不要再次阻塞。
只有缺失或冲突会让主体、用途、构图、风格、精确文字、参考图用途或主体动作产生明显不同结果时，才判定需要澄清。不要为了补齐所有常见参数而阻塞；不影响核心意图的衔接细节可采用克制、专业且不抢戏的默认选择。
若需要澄清，先完整核对会话，筛掉不会明显改变结果的低影响细节，把本轮能够识别的主问题以及答案会触发的可预见的条件分支，在同一条回复中一次性输出。问题宁少勿多，最多四个主题问题；不得把已经能识别的问题拆到后续轮次，也不要为了凑满四个补充问题。只有用户回答后才出现、事先无法合理列出的新事实或冲突，才允许追加追问。
questions 数组的每一项只放一个主题问题，但可以在同一字符串内列出该主题的可预见条件分支。适合枚举时，换行列出“A.、B.、C.、D.……”选项，标明一个“（推荐）”，并把最后一项写为“其他（请自定义）”；存在依赖时用“若选 A，再回答 1A：……”明确列出分支，要求用户同轮回答，不要先问 A 再在下一轮反问 A1。无法合理枚举时直接要求填写具体内容。用户也可以自由输入或回答“你决定”；此时禁止输出半成品提示词。
若需求已足够明确，{target}，准确描述主体、动作、环境、构图、镜头、光线、材质、色彩和风格，不要堆砌互相冲突的关键词。summary_zh 要让用户能够核对所有关键事实、授权决定与精确限制。
若用户明确提出画幅、宽高比或输出分辨率，必须把它写入 canvas_request；width、height 使用整数像素，aspect_ratio 使用约分后的“宽:高”格式。没有明确提出时 canvas_request 使用空对象，不得臆造尺寸。canvas_request 只表达用户意图，不代表已经覆盖工作站尺寸。
当前任务是静态图片。请遵循以下工作站创作指导：
{self.workspace_prompt.strip()}{conversation_section}{generation_section}

{direction_section}

        {case_section}{retrieval_section}{edit_section}{series_section}

{PROMPT_CRAFT_GUIDANCE.strip()}

ready 时还必须完成一次交付前审查：
- 若本次输入包含候选图片，必须结合用户语义决定 reference_usage：generation 表示图片必须作为最终生图输入，analysis_only 表示图片只用于分析理解；没有候选图片时使用 none。reference_reason 用一句中文解释判断依据。不得仅因上传了图片就机械选择 generation，也不得在用户要求仿照、延续、修改或保持图片特征时静默丢图。
- creative_direction 必须是给定目录中的一个 ID；用户锁定方向时不得改选。
- template_id 必须是目录中的一个模板 ID；确实没有近似模板时使用 custom。gallery_categories 必须从 Gallery Atlas 中选择 1 个，只有不可替代的混合任务才选择 2～3 个；style_tags、scene_tags 必须从目录值中选择。selection_reason 用一句中文解释方向、Gallery 类别和模板的匹配依据。
- img2img 必须选择最贴近唯一编辑目标的 edit_recipe_id；text2img 的 edit_recipe_id 使用空字符串。不得让第三方案例覆盖当前用户需求，也不得复制其中的专有名词、品牌、IP、人物、艺术家或工作室。
- brief 必须把模糊会话压缩成交付物、用途、主体、构图、风格、精确文字、参考图计划、保持项、改变项和禁止项。img2img 的 reference_plan 必须按图片编号逐张写明职责；没有的内容使用空字符串或空数组，不得臆造。
- production_spec 必须把所选游戏模板需要的制作字段结构化；非游戏任务没有对应字段时使用空字符串、空数组或 0，不得臆造。
- game_art 的角色设定表若包含白发、疤痕、单侧护甲或机械臂等非对称特征，production_spec.directional_identity_map 必须逐项写成“面板/视图：角色侧别 → 观看者侧别 → 可见特征”，覆盖 FRONT、SIDE、BACK 和 FACE；没有非对称特征时使用空数组。
- hard_checks 只列能从最终图片判断的 2～6 个硬门槛，例如精确文字、主体数量、必要元素、参考图身份、非目标区域保持和禁止额外内容。
- exploration_plan 必须给出 4 个仍然满足同一交付物的受控方案。每个方案只允许变化 1～2 个维度，delta 使用 1～4 条具体变化；不得改变主体身份、产品外形、精确文字、参考图职责、画幅、模板或硬门槛。四个方案不能只是同义改写。
- series_contract 用于后续系列图片保持一致，只提炼可复用的身份锚点、视觉语言、色板材质、构图规则、排版规则和保持项；不要把本张图片专属文案或场景写成永久锁定项。当前工作站已有 series_contract 时必须原样沿用，不得重新定义。
- quality_hint 只能是 low、medium 或 high，默认使用 high；用户明确要求草稿探索或方向精修时，才分别使用 low 或 medium。生成时沿用工作站保存的阶段。
为了让界面尽早展示可见回复，图像创作 JSON 的顶层字段顺序必须固定：needs_clarification 依次先输出 status、questions；ready 依次先输出 status、summary_zh、prompt；其余字段随后输出。
{output_contract}JSON 字段名称只能出现一次，字段类型必须与示例一致，不得用 null 代替字符串、数组或对象。图像创作严格使用以下格式之一：
{{"status":"needs_clarification","questions":["问题 1","问题 2"],"creative_direction":"poster"}}
{{"status":"ready","summary_zh":"中文需求确认","prompt":"最终生图提示词","canvas_request":{{"aspect_ratio":"16:9","width":1920,"height":1080}},"reference_usage":"generation","reference_reason":"用户要求保持参考图主体并修改背景。","creative_direction":"poster","template_id":"poster-layout-system","edit_recipe_id":"","gallery_categories":["typography-and-posters"],"style_tags":["Poster"],"scene_tags":["Commerce"],"selection_reason":"交付物是商业海报，匹配排版与海报图谱及海报排版模板。","brief":{{"deliverable":"交付物","intended_use":"用途与受众","subject":"主体","composition":"构图与画幅","style":"媒介、材质、光线与配色","exact_text":["必须逐字出现的文字"],"reference_plan":[{{"image_number":1,"role":"职责","preserve":["保持项"],"change":["改变项"]}}],"preserve":["全局保持项"],"change":["改变项"],"avoid":["禁止项"]}},"production_spec":{{"platform":"平台","canvas":"画布","screen_type":"界面或交付物状态","safe_area":"安全区","hud_zones":["区域职责"],"panel_count":0,"panel_roles":["面板职责"],"identity_anchors":["身份锚点"],"camera_and_action":"镜头与动作","materials":["材质"],"palette_and_lighting":"色板与光线","exact_text":["必须逐字出现的文字"],"ui_constraints":["界面约束"],"consistency_rules":["一致性规则"]}},"exploration_plan":[{{"label":"中心层级","delta":["主体采用中心英雄构图","使用冷蓝银色色板"]}},{{"label":"非对称留白","delta":["主体采用三分法构图","右侧保留呼吸空间"]}},{{"label":"材质近景","delta":["镜头更接近主体","强化材质与侧光"]}},{{"label":"环境叙事","delta":["使用更宽的环境构图","增加前中后景层次"]}}],"series_contract":{{"identity_anchors":["系列主体身份"],"visual_language":["统一视觉语言"],"palette_materials":["统一色板和材质"],"composition_rules":["统一构图语法"],"typography_rules":["统一字体层级"],"must_preserve":["跨图保持项"],"allowed_changes":["文案、动作和场景内容"]}},"hard_checks":["可从成品判断的硬门槛"],"quality_hint":"high"}}"""

    def parse(self, content: str) -> dict[str, Any]:
        payload = parse_json_object(content)
        if payload is not None:
            status = _draft_status(payload)
            parser = {
                "conversation": self._conversation,
                "needs_clarification": self._clarification,
                "ready": self._ready,
            }.get(status)
            parsed = parser(payload) if parser else None
            if parsed is not None:
                return parsed
        if self.conversation_prompt.strip() and _plain_text_response(content):
            if visible := _text(content, self.max_prompt_characters):
                return self._conversation_result(visible)
        raise ServiceError(
            "聊天模型未能返回有效提示词草稿，请重试",
            code=PROMPT_DRAFT_INVALID_OUTPUT_CODE,
            status_code=502,
        )

    def conversation_fallback(self, *contents: str) -> dict[str, Any] | None:
        if not self.conversation_prompt.strip():
            return None
        for content in contents:
            visible = _visible_response_text(content, self.max_prompt_characters)
            if visible:
                return self._conversation_result(visible)
        return self._conversation_result(
            "我已经收到你的消息，但这次没能整理成可直接生成的提示词。"
            "你可以继续描述想法，或让我重新整理当前需求。"
        )

    def finalize(
        self,
        draft: dict[str, Any],
        *,
        generation_mode: str,
        reference_ids: list[str],
    ) -> dict[str, Any]:
        result = dict(draft)
        candidate_ids = list(reference_ids)
        if result.get("status") == "conversation":
            result.update(
                {
                    "reference_usage": "analysis_only" if candidate_ids else "none",
                    "generation_mode": generation_mode,
                    "reference_ids": [],
                }
            )
            return result
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
        if draft["status"] == "conversation":
            return draft["message"], "message"
        if draft["status"] == "needs_clarification":
            questions = "\n".join(
                f"{index}. {question}" for index, question in enumerate(draft["questions"], 1)
            )
            return f"为了让生成结果更符合预期，还需要确认：\n{questions}", "message"
        label = "English prompt" if self.translate_to_english else "生图提示词"
        return f"需求确认\n{draft['summary_zh']}\n\n{label}\n{draft['prompt']}", "prompt_draft"

    def _conversation(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.conversation_prompt.strip():
            return None
        message = next(
            (
                text
                for key in _VISIBLE_MESSAGE_NESTING_KEYS
                if (text := _visible_message(payload.get(key), self.max_prompt_characters))
            ),
            _visible_message(payload, self.max_prompt_characters),
        )
        if not message:
            return None
        return self._conversation_result(message)

    def _conversation_result(self, message: str) -> dict[str, Any]:
        return {
            "status": "conversation",
            "message": message,
            "language": "en" if self.translate_to_english else "zh",
        }

    def _clarification(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        raw_questions = payload.get("questions")
        if isinstance(raw_questions, str):
            raw_questions = [raw_questions]
        if not isinstance(raw_questions, list):
            return None
        questions = _string_list(raw_questions, 4, 2000)
        if not questions:
            return None
        return {
            "status": "needs_clarification",
            "questions": questions,
            "language": "en" if self.translate_to_english else "zh",
            "creative_direction": _direction_id(
                payload.get("creative_direction"),
                self.creative_direction_id,
                self.gallery_category_id,
            ),
            "reference_usage": _reference_usage(payload.get("reference_usage")),
            "reference_reason": _text(payload.get("reference_reason"), 500),
            "sources": [dict(source) for source in SOURCE_METADATA],
        }

    def _ready(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        summary = _text(
            payload.get("summary_zh") or payload.get("summary"),
            self.max_prompt_characters,
        )
        prompt = _text(
            payload.get("prompt") or payload.get("final_prompt"),
            self.max_prompt_characters,
        )
        if not summary or not prompt:
            return None
        direction_id = _direction_id(
            payload.get("creative_direction"),
            self.creative_direction_id,
            self.gallery_category_id,
        )
        catalog = _catalog_selection(payload, direction_id, self.gallery_category_id)
        direction = get_creative_direction(direction_id)
        reference_usage = _reference_usage(payload.get("reference_usage"))
        edit_enabled = self.generation_mode == "img2img" or (
            self.generation_mode == "auto"
            and self.reference_count > 0
            and reference_usage not in {"analysis_only", "none"}
        )
        edit_recipe = EDIT_RECIPES.select(
            payload.get("edit_recipe_id"),
            enabled=edit_enabled,
        )
        brief = _brief(payload.get("brief"), reference_count=self.reference_count)
        production_spec = _production_spec(payload.get("production_spec"))
        exploration_plan = _exploration_plan(
            payload.get("exploration_plan"),
            direction_id=direction_id,
        )
        series_contract = _series_contract(
            self.active_series_contract or payload.get("series_contract"),
            brief=brief,
            production_spec=production_spec,
        )
        prompt = _enforce_prompt_contract(
            prompt,
            brief=brief,
            production_spec=production_spec,
            style_tags=catalog.get("style_tags", []),
            generation_mode="img2img" if edit_enabled else "text2img",
            translate_to_english=self.translate_to_english,
            maximum=self.max_prompt_characters,
        )
        template_checks = catalog.get("template_hard_checks", [])
        direction_checks = list(direction.hard_checks) if direction else []
        edit_checks = list(edit_recipe.hard_checks) if edit_recipe else []
        exact_text_check = _exact_text_hard_check(brief, production_spec)
        requested_checks = [
            *([exact_text_check] if exact_text_check else []),
            *edit_checks,
            *_string_list(payload.get("hard_checks"), 6, 300),
        ]
        return {
            "status": "ready",
            "summary_zh": summary,
            "prompt": prompt,
            "canvas_request": normalize_canvas_request(payload.get("canvas_request")),
            "language": "en" if self.translate_to_english else "zh",
            "creative_direction": direction_id,
            "reference_usage": reference_usage,
            "reference_reason": _text(payload.get("reference_reason"), 500),
            **catalog,
            **EDIT_RECIPES.metadata(edit_recipe),
            "retrieved_cases": CASE_CATALOG.metadata(self.retrieved_cases),
            "retrieval_confidence": self.retrieval_confidence,
            "retrieval_reason": self.retrieval_reason,
            "brief": brief,
            "production_spec": production_spec,
            "exploration_plan": exploration_plan,
            "series_contract": series_contract,
            "hard_checks": _merge_hard_checks(
                requested_checks,
                [*template_checks, *direction_checks],
            ),
            "quality_hint": _quality_hint(payload.get("quality_hint")),
            "sources": [dict(source) for source in SOURCE_METADATA],
        }


def _direction_id(value: Any, fallback: str, gallery_category_id: str = "auto") -> str:
    locked_direction = str(fallback or "auto").strip().lower()
    direction_id = (
        locked_direction if locked_direction != "auto" else str(value or "other").strip().lower()
    )
    if direction_id == "auto":
        direction_id = "other"
    try:
        get_creative_direction(direction_id)
    except ValueError:
        direction_id = "other"
    gallery_category = GALLERY_ATLAS.get(gallery_category_id)
    if gallery_category is not None and not GALLERY_ATLAS.compatible(
        gallery_category.identifier,
        direction_id,
    ):
        if locked_direction == "auto":
            return gallery_category.direction_ids[0] if gallery_category.direction_ids else "other"
        raise ServiceError(
            "锁定的 Gallery 类别与创作方向不兼容，请重新选择",
            code="creative_catalog_conflict",
            status_code=409,
        )
    return direction_id


def _catalog_selection(
    payload: dict[str, Any],
    direction_id: str,
    gallery_category_id: str = "auto",
) -> dict[str, Any]:
    template_id = normalize_template_id(payload.get("template_id"), direction_id)
    template = get_prompt_template(template_id)
    locked_gallery = GALLERY_ATLAS.get(gallery_category_id)
    if template is not None and locked_gallery is not None:
        if locked_gallery.identifier not in GALLERY_ATLAS.for_template(template):
            template_id = "custom"
            template = None
    style_tags = normalize_catalog_tags(payload.get("style_tags"))
    scene_tags = normalize_catalog_tags(payload.get("scene_tags"), scene=True)
    if template is not None:
        if not style_tags:
            style_tags = list(template.styles)
        if not scene_tags:
            scene_tags = list(template.scenes)
    case_refs = []
    if template is not None:
        case_refs = list(template.case_refs) or [
            f"awesome:{case_id}" for case_id in template.example_case_ids
        ]
    gallery_categories = (
        [locked_gallery.identifier]
        if locked_gallery is not None
        else GALLERY_ATLAS.select(
            payload.get("gallery_categories"),
            preferred=template.gallery_categories if template else (),
            case_refs=case_refs,
            direction_id=direction_id,
        )
    )
    return {
        "template_id": template_id,
        "template_label": template.label if template else "自定义 Craft",
        "case_refs": case_refs,
        **GALLERY_ATLAS.metadata(gallery_categories),
        "template_required_fields": list(template.required_fields) if template else [],
        "template_hard_checks": list(template.hard_checks) if template else [],
        "style_tags": style_tags,
        "style_labels": catalog_tag_labels(style_tags),
        "scene_tags": scene_tags,
        "scene_labels": catalog_tag_labels(scene_tags, scene=True),
        "selection_reason": _text(payload.get("selection_reason"), 500),
    }


def _quality_hint(value: Any) -> str:
    normalized = str(value or "high").strip().lower()
    return normalized if normalized in {"low", "medium", "high"} else "high"


def _reference_usage(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"generation", "analysis_only", "none"} else ""


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    text = _HIDDEN_REASONING_BLOCK.sub("", value)
    text = _HIDDEN_REASONING_CLOSE.sub("", text)
    return text.strip()[:maximum]


def _visible_message(value: Any, maximum: int, depth: int = 0) -> str:
    if depth > 2:
        return ""
    if text := _text(value, maximum):
        return text
    if not isinstance(value, dict):
        return ""
    for key in (*_VISIBLE_MESSAGE_KEYS, *_VISIBLE_MESSAGE_NESTING_KEYS):
        if text := _visible_message(value.get(key), maximum, depth + 1):
            return text
    return ""


def _string_list(value: Any, limit: int, maximum: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = _text(item, maximum)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _merge_hard_checks(value: Any, defaults: list[str]) -> list[str]:
    result = _string_list(value, 6, 300)
    for item in defaults:
        if len(result) >= 6:
            break
        text = str(item).strip()[:300]
        if text and text not in result:
            result.append(text)
    return result


def _brief(value: Any, *, reference_count: int = 0) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    brief = {
        key: _text(raw.get(key), 500)
        for key in ("deliverable", "intended_use", "subject", "composition", "style")
    }
    for key in ("exact_text", "preserve", "change", "avoid"):
        brief[key] = _string_list(raw.get(key), 12, 500)
    reference_plan = []
    seen_image_numbers: set[int] = set()
    raw_plan = raw.get("reference_plan")
    if reference_count > 0 and isinstance(raw_plan, list):
        for item in raw_plan[:8]:
            if not isinstance(item, dict):
                continue
            try:
                image_number = max(1, min(reference_count, int(item.get("image_number", 1))))
            except (TypeError, ValueError):
                image_number = 1
            if image_number in seen_image_numbers:
                continue
            seen_image_numbers.add(image_number)
            reference_plan.append(
                {
                    "image_number": image_number,
                    "role": _text(item.get("role"), 300),
                    "preserve": _string_list(item.get("preserve"), 8, 300),
                    "change": _string_list(item.get("change"), 8, 300),
                }
            )
    brief["reference_plan"] = reference_plan
    return brief


def _enforce_prompt_contract(
    prompt: str,
    *,
    brief: dict[str, Any],
    production_spec: dict[str, Any],
    style_tags: list[str],
    generation_mode: str,
    translate_to_english: bool,
    maximum: int,
) -> str:
    contract: dict[str, Any] = {}
    exact_text = _exact_text_values(brief, production_spec)
    if exact_text:
        contract["exact_text"] = [
            {"verbatim": text, "occurrences": 1, "extra_characters": False} for text in exact_text
        ]
    if generation_mode == "img2img":
        visual_style = _text(brief.get("style"), 500)
        normalized_style_tags = _string_list(style_tags, 4, 80)
        if normalized_style_tags or visual_style:
            contract["target_visual_style"] = {
                "style_tags": normalized_style_tags,
                "description": visual_style,
            }
        if brief.get("reference_plan"):
            contract["reference_roles"] = brief["reference_plan"]
        for source_key, target_key in (
            ("change", "change_only"),
            ("preserve", "must_preserve"),
            ("avoid", "must_avoid"),
        ):
            if brief.get(source_key):
                contract[target_key] = brief[source_key]
    prompt_production_spec = {
        key: value for key, value in production_spec.items() if key != "exact_text"
    }
    if prompt_production_spec:
        contract["production_spec"] = prompt_production_spec
    if not contract:
        return prompt
    heading = (
        "Production contract (must be followed literally):"
        if translate_to_english
        else "制作契约（必须逐项执行）："
    )
    block = f"{heading}\n{json.dumps(contract, ensure_ascii=False, indent=2)}"
    result = f"{prompt.rstrip()}\n\n{block}"
    if len(result) > maximum:
        raise ServiceError(
            f"结构化制作契约加入后提示词超过 {maximum} 个字符，请精简需求",
            code=PROMPT_CONTRACT_TOO_LONG_CODE,
            status_code=422,
        )
    return result


def _exact_text_values(brief: dict[str, Any], production_spec: dict[str, Any]) -> list[str]:
    result = []
    for value in (*brief.get("exact_text", []), *production_spec.get("exact_text", [])):
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result[:12]


def _exact_text_hard_check(brief: dict[str, Any], production_spec: dict[str, Any]) -> str:
    values = _exact_text_values(brief, production_spec)
    if not values:
        return ""
    rendered = " / ".join(f'"{value}"' for value in values)
    return f"以下文字逐字正确、各出现一次且没有额外字符：{rendered}"[:300]


_STRUCTURED_DIRECTIONS = {"document", "game_ui", "infographic", "ui"}
_STRUCTURED_EXPLORATION = (
    ("均衡网格", ("采用均衡模块网格和清晰主次层级", "保持信息路径简洁直接")),
    ("非对称模块", ("采用受控的非对称模块布局", "用留白强化最重要的信息区")),
    ("紧凑信息", ("提高有效信息密度但保持分组清晰", "强化状态、标签和关键数据的对比")),
    ("宽松展示", ("扩大区块间距和视觉呼吸空间", "使用更克制的装饰与强调色")),
)
_VISUAL_EXPLORATION = (
    ("中心层级", ("采用中心英雄构图和明确焦点", "沿用基础色板并增强主体轮廓")),
    ("非对称留白", ("采用三分法或受控非对称构图", "为关键信息保留明确呼吸空间")),
    ("材质近景", ("使用更接近主体的镜头", "强化材质细节与方向性侧光")),
    ("环境叙事", ("使用更宽的环境构图", "增加前景、中景和背景的空间层次")),
)


def _exploration_plan(value: Any, *, direction_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value[:4]:
            if not isinstance(item, dict):
                continue
            label = _text(item.get("label"), 80)
            delta = _string_list(item.get("delta"), 4, 300)
            if not label or not delta or any(entry["label"] == label for entry in result):
                continue
            result.append({"label": label, "delta": delta})
    fallbacks = (
        _STRUCTURED_EXPLORATION if direction_id in _STRUCTURED_DIRECTIONS else _VISUAL_EXPLORATION
    )
    for label, delta in fallbacks:
        if len(result) >= 4:
            break
        if any(entry["label"] == label for entry in result):
            continue
        result.append({"label": label, "delta": list(delta)})
    return result[:4]


def _series_contract(
    value: Any,
    *,
    brief: dict[str, Any],
    production_spec: dict[str, Any],
) -> dict[str, list[str]]:
    raw = value if isinstance(value, dict) else {}
    keys = (
        "identity_anchors",
        "visual_language",
        "palette_materials",
        "composition_rules",
        "typography_rules",
        "must_preserve",
        "allowed_changes",
    )
    result = {key: _string_list(raw.get(key), 6, 300) for key in keys}
    if not result["identity_anchors"]:
        result["identity_anchors"] = _unique_strings(
            [brief.get("subject", ""), *production_spec.get("identity_anchors", [])],
            6,
        )
    if not result["visual_language"]:
        result["visual_language"] = _unique_strings([brief.get("style", "")], 6)
    if not result["palette_materials"]:
        result["palette_materials"] = _unique_strings(
            [
                production_spec.get("palette_and_lighting", ""),
                production_spec.get("palette", ""),
                *production_spec.get("materials", []),
            ],
            6,
        )
    if not result["composition_rules"]:
        result["composition_rules"] = _unique_strings(
            [
                brief.get("composition", ""),
                production_spec.get("camera_and_action", ""),
                *production_spec.get("consistency_rules", []),
            ],
            6,
        )
    if not result["typography_rules"] and _exact_text_values(brief, production_spec):
        result["typography_rules"] = ["沿用统一字体家族、字号层级和文字对齐规则"]
    if not result["must_preserve"]:
        result["must_preserve"] = _unique_strings(
            [*brief.get("preserve", []), *production_spec.get("consistency_rules", [])],
            6,
        )
    if not result["allowed_changes"]:
        result["allowed_changes"] = ["每张图片的具体文案、主体动作和场景内容"]
    return {key: values for key, values in result.items() if values}


def _unique_strings(values: list[Any], limit: int) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()[:300]
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _production_spec(value: Any) -> dict[str, Any]:
    """Keep a compact, typed production contract for game UI and concept work."""
    raw = value if isinstance(value, dict) else {}
    text_keys = (
        "platform",
        "canvas",
        "screen_type",
        "safe_area",
        "navigation",
        "selected_state",
        "grid",
        "display_size",
        "map_scale",
        "orientation",
        "deliverable_stage",
        "world_and_faction",
        "environment_function",
        "player_route",
        "scale_reference",
        "camera",
        "camera_and_action",
        "lighting",
        "palette",
        "palette_and_lighting",
        "prop_function",
        "user_and_scale",
        "views",
        "material_breakdown",
        "camera_axis",
        "asset_module",
        "atomic_asset",
        "asset_type",
        "transparent_output",
        "nine_slice",
    )
    list_keys = (
        "hud_zones",
        "ui_states",
        "grid_and_slots",
        "markers_and_legend",
        "quest_text",
        "icon_roles",
        "identity_anchors",
        "directional_identity_map",
        "views_and_expressions",
        "costume_and_equipment",
        "landmarks",
        "materials",
        "mechanical_details",
        "callouts",
        "panel_roles",
        "shared_identity_anchors",
        "shared_palette",
        "shared_materials",
        "labels",
        "frame_roles",
        "action_beats",
        "effects",
        "exact_text",
        "ui_constraints",
        "consistency_rules",
        "component_tree",
        "runtime_content",
        "asset_states",
        "reconstruction_rules",
    )
    result: dict[str, Any] = {}
    for key in text_keys:
        text = _text(raw.get(key), 500)
        if text:
            result[key] = text
    for key in list_keys:
        values = _string_list(raw.get(key), 12, 300)
        if values:
            result[key] = values
    for key in ("panel_count", "icon_count", "frame_count"):
        try:
            count = max(0, min(64, int(raw.get(key, 0))))
        except (TypeError, ValueError):
            count = 0
        if count:
            result[key] = count
    return result
