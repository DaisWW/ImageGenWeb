from __future__ import annotations

import json
import re
from typing import Any

from ..errors import ServiceError


class PromptDraftParser:
    @staticmethod
    def system_prompt(*, translate_to_english: bool) -> str:
        target = (
            "prompt 必须是自然、具体、结构清晰的英文生图提示词"
            if translate_to_english
            else "prompt 必须是自然、具体、结构清晰的中文生图提示词"
        )
        return f"""你是高级 AI 生图提示词工程师。根据会话中已经确认的信息，为 GPT Image 2 整理最终提示词。
{target}，准确描述主体、动作、环境、构图、镜头、光线、材质、色彩和风格，不要堆砌互相冲突的关键词。
summary_zh 用于用户核对语义；没有确认的信息不要擅自补成关键事实，可以采用保守中性表达。
只输出一个 JSON 对象，不要 Markdown，不要额外说明，格式为：
{{"summary_zh":"中文需求确认","prompt":"最终生图提示词"}}"""

    @staticmethod
    def parse(content: str, *, translate_to_english: bool) -> dict[str, Any]:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(
                r"^```(?:json)?\s*|\s*```$",
                "",
                cleaned,
                flags=re.IGNORECASE,
            )
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(cleaned[start : end + 1])
                summary = str(payload.get("summary_zh", "")).strip()
                prompt = str(payload.get("prompt", "")).strip()
                if summary and prompt:
                    return {
                        "summary_zh": summary[:8000],
                        "prompt": prompt[:8000],
                        "language": "en" if translate_to_english else "zh",
                    }
            except (TypeError, ValueError):
                pass
        raise ServiceError("聊天模型未能返回有效提示词草稿，请重试")
