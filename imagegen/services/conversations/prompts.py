from collections.abc import Mapping

from ...config.chat_models import DEFAULT_SYSTEM_PROMPTS
from ...validation import as_bool

CHAT_SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPTS["chat"]
SUMMARY_SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPTS["summary"]


def generation_mode_prompt(
    workspace_kind: str,
    mode: str,
    reference_count: int,
) -> str:
    """给对话和总结模型同一份生成模式/参考图契约。"""
    if workspace_kind == "animation":
        return """当前任务固定为 img2img，母图必须由用户指定，禁止生成母图或切换为文生图。
所有沟通和提示词只针对帧动画；参考图 1 是身份、造型、配色、构图和镜头基准。"""

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


def animation_runtime_prompt(
    workspace_kind: str,
    settings: Mapping[str, object] | None,
) -> str:
    if workspace_kind != "animation":
        return ""
    values = settings if isinstance(settings, Mapping) else {}
    try:
        frame_count = max(2, min(100, int(values.get("animation_frame_count", 8))))
    except (TypeError, ValueError):
        frame_count = 8
    try:
        fps = max(1, min(60, int(values.get("animation_fps", 8))))
    except (TypeError, ValueError):
        fps = 8
    loop = as_bool(values.get("animation_loop", True))
    denominator = frame_count if loop else max(1, frame_count - 1)
    phase_end = frame_count - 1
    phase_end = phase_end / denominator * 100
    mode = (
        "循环：末帧应自然衔接回第 1 帧，不能复制第 1 帧"
        if loop
        else "单次播放：第 1 帧到末帧完成一次动作，末帧可停留"
    )
    return (
        f"帧数：{frame_count} 帧；帧率：{fps} FPS；单帧时长：{1000 / fps:.1f} ms；"
        f"总时长：{frame_count / fps:.3f} 秒。\n"
        f"{mode}。相位从第 1 帧 0.0% 递进到第 {frame_count} 帧 "
        f"{phase_end:.1f}%；每一帧只呈现该相位，不要把多个相位画在同一张图中。"
    )
