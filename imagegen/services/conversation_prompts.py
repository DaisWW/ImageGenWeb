from collections.abc import Mapping

from ..config.chat_models import DEFAULT_SYSTEM_PROMPTS
from ..validation import as_bool

CHAT_SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPTS["chat"]
SUMMARY_SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPTS["summary"]


def chat_system_prompt(
    base_prompt: str,
    workspace_prompt: str,
    runtime_prompt: str = "",
) -> str:
    sections = [
        base_prompt.strip(),
        f"当前工作站的创作指导如下：\n{workspace_prompt.strip()}",
    ]
    if runtime_prompt.strip():
        sections.append(f"本次任务的运行参数如下：\n{runtime_prompt.strip()}")
    return "\n\n".join(sections)


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
