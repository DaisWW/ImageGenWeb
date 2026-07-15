from __future__ import annotations

import json
import re
from typing import Any

from ..errors import ServiceError


class PromptDraftParser:
    @staticmethod
    def system_prompt(
        *,
        translate_to_english: bool,
        workspace_kind: str = "image",
        workspace_prompt: str,
        runtime_prompt: str = "",
    ) -> str:
        target = (
            "prompt 必须是自然、具体、结构清晰的英文生图提示词"
            if translate_to_english
            else "prompt 必须是自然、具体、结构清晰的中文生图提示词"
        )
        task = "帧动画" if workspace_kind == "animation" else "静态图片"
        runtime_section = (
            f"\n本次任务的运行参数如下：\n{runtime_prompt.strip()}"
            if runtime_prompt.strip()
            else ""
        )
        return f"""你是高级 AI 生图需求审查员与提示词工程师。先判断会话是否已经足够明确，再决定是继续澄清还是为 GPT Image 2 整理最终提示词。
独立核对用户已经确认的事实、用户明确授权 AI 决定的事项、未解决问题和互相冲突的要求。助手曾提出但用户没有接受的建议不能视为已确认；用户明确回答“你决定”或同义表达时，该项视为已授权，不要再次阻塞。
只有缺失或冲突会让主体、用途、构图、风格、精确文字、参考图用途或动画动作产生明显不同结果时，才判定需要澄清。不要为了补齐所有常见参数而阻塞；不影响核心意图的衔接细节可采用克制、专业且不抢戏的默认选择。
若需要澄清，输出一到三个信息增益最高、互不重复且容易回答的问题。适合时在问题中给出二到四个差异明确的选项和一个推荐项，并允许用户回答“你决定”；此时禁止输出半成品提示词。
若需求已足够明确，{target}，准确描述主体、动作、环境、构图、镜头、光线、材质、色彩和风格，不要堆砌互相冲突的关键词。summary_zh 要让用户能够核对所有关键事实、授权决定与精确限制。
当前任务是{task}。请遵循以下工作站创作指导：
{workspace_prompt.strip()}{runtime_section}
只输出一个 JSON 对象，不要 Markdown，不要额外说明，并严格使用以下两种格式之一：
{{"status":"needs_clarification","questions":["问题 1","问题 2"]}}
{{"status":"ready","summary_zh":"中文需求确认","prompt":"最终生图提示词"}}"""

    @staticmethod
    def parse(
        content: str,
        *,
        translate_to_english: bool,
        max_prompt_characters: int = 8000,
    ) -> dict[str, Any]:
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
                if not isinstance(payload, dict):
                    raise ValueError
                status = str(payload.get("status", "")).strip().lower()
                raw_questions = payload.get("questions")
                if status == "needs_clarification" and isinstance(raw_questions, list):
                    questions: list[str] = []
                    for item in raw_questions:
                        if not isinstance(item, str):
                            continue
                        question = item.strip()
                        if question and question not in questions:
                            questions.append(question[:500])
                    if questions:
                        return {
                            "status": "needs_clarification",
                            "questions": questions[:3],
                            "language": "en" if translate_to_english else "zh",
                        }
                summary = str(payload.get("summary_zh", "")).strip()
                prompt = str(payload.get("prompt", "")).strip()
                if status == "ready" and summary and prompt:
                    return {
                        "status": "ready",
                        "summary_zh": summary[:max_prompt_characters],
                        "prompt": prompt[:max_prompt_characters],
                        "language": "en" if translate_to_english else "zh",
                    }
            except (TypeError, ValueError):
                pass
        raise ServiceError("聊天模型未能返回有效提示词草稿，请重试")
