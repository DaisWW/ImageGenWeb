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
        generation_prompt: str = "",
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
        generation_section = f"\n{generation_prompt.strip()}" if generation_prompt.strip() else ""
        return f"""你是高级 AI 生图需求审查员与提示词工程师。先判断会话是否已经足够明确，再决定是继续澄清还是为 GPT Image 2 整理最终提示词。
独立核对用户已经确认的事实、用户明确授权 AI 决定的事项、未解决问题和互相冲突的要求。助手曾提出但用户没有接受的建议不能视为已确认；用户明确回答“你决定”或同义表达时，该项视为已授权，不要再次阻塞。
只有缺失或冲突会让主体、用途、构图、风格、精确文字、参考图用途或动画动作产生明显不同结果时，才判定需要澄清。不要为了补齐所有常见参数而阻塞；不影响核心意图的衔接细节可采用克制、专业且不抢戏的默认选择。
若需要澄清，先完整核对会话，筛掉不会明显改变结果的低影响细节，只保留信息增益最高、互不重复且容易回答的阻塞性问题。把当前能够识别的问题一次性输出，问题宁少勿多，最多四个；不得把已经能识别的问题拆到后续轮次，也不要为了凑满四个补充问题。只有用户回答后新出现、且此前无法判断的关键分支或冲突，才允许追加追问。
questions 数组的每一项只放一个问题。适合枚举时，在同一字符串内换行列出“A.、B.、C.、D.……”选项，标明一个“（推荐）”，并把最后一项写为“其他（请自定义）”；无法合理枚举时直接要求填写具体内容。用户也可以自由输入或回答“你决定”；此时禁止输出半成品提示词。
若需求已足够明确，{target}，准确描述主体、动作、环境、构图、镜头、光线、材质、色彩和风格，不要堆砌互相冲突的关键词。summary_zh 要让用户能够核对所有关键事实、授权决定与精确限制。
当前任务是{task}。请遵循以下工作站创作指导：
{workspace_prompt.strip()}{runtime_section}{generation_section}
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
                            "questions": questions[:4],
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
