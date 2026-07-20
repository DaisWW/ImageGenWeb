from ...config.chat_models import DEFAULT_SYSTEM_PROMPTS

CHAT_SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPTS["chat"]


def generation_mode_prompt(
    mode: str,
    reference_count: int,
) -> str:
    """给对话模型提供生成模式和参考图契约。"""
    count = max(0, int(reference_count))
    normalized_mode = mode if mode in {"auto", "img2img"} else "text2img"
    if normalized_mode == "auto" and count:
        return f"""当前收到 {count} 张候选图片。它们会提供给你理解本轮需求，但不一定要作为最终生图输入。
必须根据用户语义判断这些图片是否必须作为最终生图输入：
- reference_usage="generation"：用户要求基于、仿照、延续、修改图片，或要求保持其中的主体身份、产品外形、姿态、构图、版式、材质、色彩、笔触或风格。
- reference_usage="analysis_only"：用户只要求分析、描述、总结图片或提炼文字提示词，明确要求独立创作或不要把原图交给生图模型。
如果用户在同一轮既上传图片又要求生成，且没有明确排除图片，优先使用 generation，避免静默丢失垫图。reference_reason 用一句中文说明依据。
选择 generation 时，最终提示词必须使用“参考图 1/参考图 2……”明确每张图的作用、必须保留和必须改变；选择 analysis_only 时，最终提示词不得包含参考图编号或 img2img 指令。"""
    if normalized_mode == "img2img" or count:
        missing = (
            "当前尚未收到任何参考图，必须先要求用户上传或选择至少一张参考图，不能返回 ready。"
            if count == 0
            else f"当前实际收到 {count} 张参考图。"
        )
        return f"""当前生成模式是 img2img（参考图生图）。{missing}
参考图是最终生成输入，不是泛化的灵感板。必须在需求中逐张建立编号与作用，并明确每张图的“必须保留”和“必须改变”：例如主体身份、产品外形、姿态、构图、版式、材质、色彩或风格。若保留范围与修改目标会互相冲突，先澄清取舍。
最终提示词必须使用“参考图 1/参考图 2……”的明确指代，先写参考图处理规则，再写目标画面与修改内容；不得把未确认的参考图细节臆造为硬要求，也不得只写“参考这张图”“基于原图优化”等不可执行的空话。"""
    return "当前生成模式是 text2img（文生图），没有参考图作为最终生成输入；不要输出参考图编号或 img2img 指令。"
