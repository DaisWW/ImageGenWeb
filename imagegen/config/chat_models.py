from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from ..validation import as_bool, bounded_int, required_string
from .base import ReloadableConfigRegistry

WORKSPACE_PROMPT_MAX_LENGTH = 12000
DEFAULT_WORKSPACE_PROMPTS = {
    "image": """当前是静态图片工作站。目标是把用户意图收敛为一张主体明确、构图完整、可直接生成的最终画面。
围绕单一成片方案推进。优先确认真正影响结果的要素：用途与画幅，主体的身份、数量、外观、动作和表情，环境与时间，构图、视角和景别，光线、色彩、材质、风格，画面文字及禁止项。不要机械盘问；已有信息充分时，主动用专业判断补足非关键的视觉衔接，但不得改写用户已确认的身份、品牌、文字或核心创意。
参考图要区分用户希望保留的是主体身份、构图、姿态、配色、材质还是整体风格；没有看清的细节不要臆造。最终提示词使用自然、具体、无冲突的描述，按“主体与动作、场景与构图、镜头与光线、色彩材质与风格、精确限制”的顺序组织，避免堆砌“杰作、最高质量”等空泛词。
只描述一张完整画面，不输出分镜、拼图或备选方案。需要文字时逐字写明内容、语言、位置、排版气质和可读性；不需要文字时明确不要额外文字、水印或标志。""",
    "animation": """当前工作站用于制作帧动画。目标是得到一组主体一致、镜头稳定、时间连续、按顺序播放自然的完整单帧，而不是若干互不相关的静态图。
先锁定贯穿所有帧的不变量：主体身份、数量、造型、比例和服装，场景布局，构图、视角和景别，镜头参数，光线方向，色彩与材质。再明确时间变化：动作起点的姿态或状态、动作过程的方向与路径、关键动作阶段、动作终点的姿态或状态、速度与节奏，以及头发、衣摆、液体、粒子等次级运动。优先单一清晰的主动作、固定镜头和短时长；确需运镜时写清方向、幅度和节奏。
同时确认帧数、帧率和是否循环，并让动作幅度与节奏适合这些参数。循环动画必须确认首尾衔接，让末帧能够自然回到首帧，避免不可逆位移、突变或状态跳跃；非循环动画则让终点动作明确且可停留。参考图要说明哪些特征必须跨帧保持。不得让人物、服装、道具、背景、光线、色调、画幅或镜头无故漂移。
最终提示词同时写清跨帧不变量，以及动作从起点经过过程到终点的变化；每次输出一张完整画面。禁止分镜表、连环画、拼图、接触表、Sprite Sheet、帧编号、字幕或把多个时间点画在同一张图中；不要使用“让画面动起来”这类不可执行的空泛描述。""",
}


@dataclass(frozen=True)
class ContextPolicy:
    compact_at_tokens: int = 24000
    max_context_tokens: int = 32000
    keep_recent_messages: int = 12

    def as_dict(self) -> dict[str, int]:
        return {
            "compact_at_tokens": self.compact_at_tokens,
            "max_context_tokens": self.max_context_tokens,
            "keep_recent_messages": self.keep_recent_messages,
        }


@dataclass(frozen=True)
class ChatModelConfig:
    identifier: str
    label: str
    enabled: bool
    base_url: str
    model: str
    reasoning_effort: str
    timeout_seconds: int
    max_output_tokens: int
    api_key: str = field(repr=False)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key) and bool(self.base_url) and bool(self.model)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "label": self.label,
            "enabled": self.enabled,
            "configured": self.configured,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }

    def editable_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "label": self.label,
            "enabled": self.enabled,
            "configured": self.configured,
            "base_url": self.base_url,
            "has_api_key": bool(self.api_key),
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class ChatModelSnapshot:
    version: str
    models: dict[str, ChatModelConfig]
    context: ContextPolicy
    prompt_draft_model_id: str
    workspace_prompts: dict[str, str]


class ChatModelRegistry(ReloadableConfigRegistry[ChatModelSnapshot]):
    """Atomically reloads OpenAI-compatible chat models and keeps keys private."""

    READ_ERROR_PREFIX = "无法读取聊天模型配置"
    LOAD_ERROR_PREFIX = "聊天模型配置加载失败"
    NOT_LOADED_MESSAGE = "聊天模型配置尚未加载"

    @property
    def context(self) -> ContextPolicy:
        self.reload_if_changed()
        with self._lock:
            return self._require_snapshot().context

    @property
    def prompt_draft_model_id(self) -> str:
        self.reload_if_changed()
        with self._lock:
            return self._require_snapshot().prompt_draft_model_id

    def workspace_prompt(self, workspace_kind: str) -> str:
        self.reload_if_changed()
        kind = "animation" if workspace_kind == "animation" else "image"
        with self._lock:
            return self._require_snapshot().workspace_prompts[kind]

    def list(self) -> list[ChatModelConfig]:
        self.reload_if_changed()
        with self._lock:
            return list(self._require_snapshot().models.values())

    def get(self, identifier: str, *, require_available: bool = True) -> ChatModelConfig:
        self.reload_if_changed()
        with self._lock:
            model = self._require_snapshot().models.get(identifier)
        if model is None:
            raise ValueError(f"不支持的聊天模型：{identifier}")
        if require_available and not model.configured:
            raise ValueError(f"{model.label} 尚未配置 URL、模型或 API Key")
        return model

    def editable_config(self) -> dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            snapshot = self._require_snapshot()
            return {
                "version": snapshot.version[:12],
                "source": self._source,
                "last_error": self._last_error,
                "models": [model.editable_dict() for model in snapshot.models.values()],
                "context": snapshot.context.as_dict(),
                "prompt_draft_model_id": snapshot.prompt_draft_model_id,
                "workspace_prompts": dict(snapshot.workspace_prompts),
            }

    def _parse(self, raw: Any, raw_bytes: bytes) -> ChatModelSnapshot:
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("对话模型配置必须包含 version: 1")
        context_raw = raw.get("context", {})
        if not isinstance(context_raw, dict):
            raise ValueError("context 配置必须是对象")
        context = ContextPolicy(
            compact_at_tokens=bounded_int(context_raw, "compact_at_tokens", 24000, 1000, 500000),
            max_context_tokens=bounded_int(context_raw, "max_context_tokens", 32000, 2000, 1000000),
            keep_recent_messages=bounded_int(context_raw, "keep_recent_messages", 12, 4, 100),
        )
        if context.compact_at_tokens >= context.max_context_tokens:
            raise ValueError("compact_at_tokens 必须小于 max_context_tokens")

        raw_models = raw.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            raise ValueError("聊天模型配置至少需要一个聊天模型")
        models: dict[str, ChatModelConfig] = {}
        for item in raw_models:
            model = self._parse_model(item)
            if model.identifier in models:
                raise ValueError(f"聊天模型 ID 重复：{model.identifier}")
            models[model.identifier] = model
        prompt_draft_model_id = str(raw.get("prompt_draft_model_id", "")).strip()
        if len(prompt_draft_model_id) > 64:
            raise ValueError("提示词整理模型 ID 不能超过 64 个字符")
        if prompt_draft_model_id and prompt_draft_model_id not in models:
            raise ValueError(f"提示词整理模型不存在：{prompt_draft_model_id}")
        raw_prompts = raw.get("workspace_prompts", {})
        if not isinstance(raw_prompts, dict):
            raise ValueError("workspace_prompts 配置必须是对象")
        workspace_prompts: dict[str, str] = {}
        for kind, default in DEFAULT_WORKSPACE_PROMPTS.items():
            value = raw_prompts.get(kind, default)
            if not isinstance(value, str):
                raise ValueError(f"{kind} 工作站提示词必须是文本")
            value = value.strip()
            if not value or len(value) > WORKSPACE_PROMPT_MAX_LENGTH:
                raise ValueError(
                    f"{kind} 工作站提示词长度必须在 1 到 {WORKSPACE_PROMPT_MAX_LENGTH} 个字符之间"
                )
            workspace_prompts[kind] = value
        return ChatModelSnapshot(
            version=hashlib.sha256(raw_bytes).hexdigest(),
            models=models,
            context=context,
            prompt_draft_model_id=prompt_draft_model_id,
            workspace_prompts=workspace_prompts,
        )

    @staticmethod
    def _parse_model(raw: Any) -> ChatModelConfig:
        if not isinstance(raw, dict):
            raise ValueError("每个聊天模型配置必须是对象")
        identifier = required_string(raw, "id", 64, section="聊天模型")
        if not identifier.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"聊天模型 ID 无效：{identifier}")
        label = required_string(raw, "label", 100, section="聊天模型")
        base_url = os.environ.get(str(raw.get("base_url_env", "")).strip(), "").strip()
        base_url = (base_url or required_string(raw, "base_url", 500, section="聊天模型")).rstrip(
            "/"
        )
        if not base_url.startswith(("https://", "http://")):
            raise ValueError(f"{label} 的 base_url 必须是 HTTP(S) 地址")
        model = os.environ.get(str(raw.get("model_env", "")).strip(), "").strip()
        model = model or required_string(raw, "model", 150, section="聊天模型")
        reasoning_effort = os.environ.get(
            str(raw.get("reasoning_effort_env", "")).strip(), ""
        ).strip()
        reasoning_effort = reasoning_effort or str(raw.get("reasoning_effort", "")).strip()
        allowed_efforts = {"", "none", "minimal", "low", "medium", "high", "max"}
        if reasoning_effort not in allowed_efforts:
            raise ValueError(f"{label} 的 reasoning_effort 配置无效")
        api_key = str(raw.get("api_key", "")).strip()
        if not api_key:
            api_key = os.environ.get(str(raw.get("api_key_env", "")).strip(), "").strip()
        return ChatModelConfig(
            identifier=identifier,
            label=label,
            enabled=as_bool(raw.get("enabled", True)),
            base_url=base_url,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=bounded_int(raw, "timeout_seconds", 180, 10, 600),
            max_output_tokens=bounded_int(raw, "max_output_tokens", 2000, 128, 16000),
            api_key=api_key,
        )
