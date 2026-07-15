from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from ..config.chat_models import ChatModelConfig
from .diagnostics import response_summary


@dataclass(frozen=True)
class ChatCompletion:
    content: str
    request_id: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    elapsed_seconds: float | None = None


class OpenAIChatError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "chat_provider_error",
        status_code: int = 502,
        request_id: str = "",
        upstream_status: int | None = None,
        elapsed_seconds: float | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.upstream_status = upstream_status
        self.elapsed_seconds = elapsed_seconds
        self.details = details or {}


class OpenAIChatClient:
    """面向兼容 OpenAI 的 Chat Completions 接口的轻量客户端。"""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def complete(
        self,
        model: ChatModelConfig,
        *,
        system: str,
        messages: list[dict[str, Any]],
        max_output_tokens: int | None = None,
    ) -> ChatCompletion:
        started = time.monotonic()
        payload = {
            "model": model.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "max_completion_tokens": max_output_tokens or model.max_output_tokens,
        }
        if model.reasoning_effort:
            payload["reasoning_effort"] = model.reasoning_effort
        try:
            response = self.session.post(
                _endpoint(model.base_url),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {model.api_key}",
                },
                json=payload,
                timeout=model.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise OpenAIChatError(
                "聊天模型响应超时",
                code="chat_timeout",
                status_code=504,
                elapsed_seconds=_elapsed(started),
                details={"exception_type": exc.__class__.__name__},
            ) from exc
        except requests.RequestException as exc:
            raise OpenAIChatError(
                "无法连接聊天模型",
                code="chat_connection_error",
                elapsed_seconds=_elapsed(started),
                details={"exception_type": exc.__class__.__name__},
            ) from exc
        if not response.ok:
            self._raise_upstream(response, elapsed_seconds=_elapsed(started))
        payload = self._json(response, elapsed_seconds=_elapsed(started))
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenAIChatError(
                "聊天服务返回了无法识别的响应",
                request_id=_request_id(response, payload),
                upstream_status=response.status_code,
                elapsed_seconds=_elapsed(started),
                details=response_summary(response, payload),
            ) from exc
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        content = str(content or "").strip()
        if not content:
            raise OpenAIChatError(
                "聊天服务返回了空内容",
                request_id=_request_id(response, payload),
                upstream_status=response.status_code,
                elapsed_seconds=_elapsed(started),
                details=response_summary(response, payload),
            )
        usage = payload.get("usage") or {}
        return ChatCompletion(
            content=content,
            request_id=_request_id(response, payload),
            input_tokens=_optional_int(usage.get("prompt_tokens")),
            output_tokens=_optional_int(usage.get("completion_tokens")),
            elapsed_seconds=_elapsed(started),
        )

    @staticmethod
    def _raise_upstream(response: requests.Response, *, elapsed_seconds: float) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        upstream = payload.get("error") if isinstance(payload, dict) else None
        detail = str(upstream.get("message") or "").strip() if isinstance(upstream, dict) else ""
        if response.status_code == 429:
            message, code = "聊天模型暂时繁忙，请稍后重试", "chat_rate_limited"
        elif response.status_code in {401, 403}:
            message, code = "聊天模型鉴权失败，请联系管理员检查配置", "chat_auth_error"
        else:
            message = detail[:300] if detail else f"聊天模型请求失败（HTTP {response.status_code}）"
            code = "chat_upstream_error"
        raise OpenAIChatError(
            message,
            code=code,
            status_code=response.status_code,
            request_id=_request_id(response, payload),
            upstream_status=response.status_code,
            elapsed_seconds=elapsed_seconds,
            details=response_summary(response, payload),
        )

    @staticmethod
    def _json(response: requests.Response, *, elapsed_seconds: float) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenAIChatError(
                "聊天服务返回了无效 JSON",
                request_id=_request_id(response),
                upstream_status=response.status_code,
                elapsed_seconds=elapsed_seconds,
                details=response_summary(response),
            ) from exc
        if not isinstance(payload, dict):
            raise OpenAIChatError(
                "聊天服务响应格式无效",
                request_id=_request_id(response),
                upstream_status=response.status_code,
                elapsed_seconds=elapsed_seconds,
                details=response_summary(response, payload),
            )
        return payload


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"


def _request_id(response: requests.Response, payload: Any = None) -> str:
    if isinstance(payload, dict) and payload.get("id"):
        return str(payload["id"])[:255]
    return str(response.headers.get("x-request-id", ""))[:255]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)
