from __future__ import annotations

import json
import threading
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
    """面向兼容 OpenAI 的 Responses 流式接口的轻量客户端。"""

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
        deadline = started + float(model.timeout_seconds)
        payload = {
            "model": model.model,
            "instructions": system,
            "input": [_responses_message(message) for message in messages],
            "stream": True,
            "max_output_tokens": max_output_tokens or model.max_output_tokens,
        }
        if model.reasoning_effort:
            payload["reasoning"] = {"effort": model.reasoning_effort}

        for attempt in range(2):
            response = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _timeout_error(started)
                response = self.session.post(
                    _endpoint(model.base_url),
                    headers={
                        "Accept": "text/event-stream",
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {model.api_key}",
                    },
                    json=payload,
                    timeout=remaining,
                    stream=True,
                )
                if time.monotonic() >= deadline:
                    raise _timeout_error(started, response=response)
                if response.status_code == 502 and attempt == 0:
                    response.close()
                    response = None
                    time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
                    continue
                if not response.ok:
                    self._raise_upstream(response, elapsed_seconds=_elapsed(started))
                return self._stream_completion(response, started=started, deadline=deadline)
            except requests.Timeout as exc:
                raise _timeout_error(
                    started,
                    exception_type=exc.__class__.__name__,
                ) from exc
            except requests.RequestException as exc:
                if time.monotonic() >= deadline:
                    raise _timeout_error(
                        started,
                        exception_type=exc.__class__.__name__,
                    ) from exc
                raise OpenAIChatError(
                    "无法连接聊天模型",
                    code="chat_connection_error",
                    elapsed_seconds=_elapsed(started),
                    details={"exception_type": exc.__class__.__name__},
                ) from exc
            finally:
                if response is not None:
                    response.close()

        raise AssertionError("unreachable")

    @staticmethod
    def _stream_completion(
        response: requests.Response,
        *,
        started: float,
        deadline: float,
    ) -> ChatCompletion:
        text_parts: list[str] = []
        terminal: dict[str, Any] | None = None
        completed: dict[str, Any] | None = None
        invalid_event_count = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timeout_error(started, response=response)
        timer = threading.Timer(remaining, _close_response, args=(response,))
        timer.daemon = True
        timer.start()

        try:
            for data in _sse_payloads(response, deadline=deadline):
                if time.monotonic() >= deadline:
                    raise requests.Timeout("聊天模型响应超过总超时限制")
                try:
                    event = json.loads(data)
                except ValueError:
                    invalid_event_count += 1
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type", "")).strip()
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        text_parts.append(delta)
                    continue
                if event_type not in {
                    "response.completed",
                    "response.done",
                    "response.failed",
                    "response.incomplete",
                    "response.cancelled",
                    "response.canceled",
                }:
                    continue
                terminal = event
                response_payload = event.get("response")
                completed = response_payload if isinstance(response_payload, dict) else {}
                if event_type not in {"response.completed", "response.done"}:
                    error = completed.get("error") or event.get("error")
                    detail = (
                        str(error.get("message", "")).strip() if isinstance(error, dict) else ""
                    )
                    raise OpenAIChatError(
                        detail or "聊天服务未能完成响应",
                        code="chat_upstream_error",
                        request_id=_request_id(response, completed),
                        upstream_status=response.status_code,
                        elapsed_seconds=_elapsed(started),
                        details=response_summary(response, completed),
                    )
                break
        except (requests.RequestException, OSError, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise _timeout_error(
                    started,
                    response=response,
                    exception_type=exc.__class__.__name__,
                ) from exc
            raise
        finally:
            timer.cancel()

        if completed is None:
            if time.monotonic() >= deadline:
                raise _timeout_error(started, response=response)
            raise OpenAIChatError(
                "聊天服务流式响应未正常结束",
                request_id=_request_id(response),
                upstream_status=response.status_code,
                elapsed_seconds=_elapsed(started),
                details={
                    "status_code": response.status_code,
                    "content_type": str(response.headers.get("content-type", ""))[:120],
                    "invalid_event_count": invalid_event_count,
                },
            )

        if time.monotonic() >= deadline:
            raise _timeout_error(started, response=response)
        content = "".join(text_parts).strip() or _response_output_text(completed).strip()
        if not content:
            raise OpenAIChatError(
                "聊天服务返回了空内容",
                request_id=_request_id(response, completed),
                upstream_status=response.status_code,
                elapsed_seconds=_elapsed(started),
                details=response_summary(response, completed),
            )
        usage = completed.get("usage") or (terminal or {}).get("usage") or {}
        return ChatCompletion(
            content=content,
            request_id=_request_id(response, completed),
            input_tokens=_optional_int(usage.get("input_tokens")),
            output_tokens=_optional_int(usage.get("output_tokens")),
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


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/responses" if base.endswith("/v1") else f"{base}/v1/responses"


def _responses_message(message: dict[str, Any]) -> dict[str, Any]:
    role = str(message.get("role", "user"))
    content = message.get("content", "")
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        parts = [{"type": text_type, "text": content}]
    else:
        parts = []
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"text", "input_text", "output_text"}:
                parts.append({"type": text_type, "text": str(item.get("text", ""))})
                continue
            if item.get("type") not in {"image_url", "input_image"}:
                continue
            image_url = item.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                parts.append({"type": "input_image", "image_url": image_url})
    return {"role": role, "content": parts}


def _sse_payloads(response: requests.Response, *, deadline: float):
    lines: list[str] = []
    for raw_line in response.iter_lines(chunk_size=1, decode_unicode=False):
        if time.monotonic() >= deadline:
            raise requests.Timeout("聊天模型响应超过总超时限制")
        line = (
            raw_line.decode("utf-8", "replace") if isinstance(raw_line, bytes) else str(raw_line)
        ).rstrip("\r\n")
        if line.startswith("data:"):
            lines.append(line[5:].lstrip())
        elif not line.strip():
            payload = _flush_sse_data(lines)
            if payload:
                yield payload
    if time.monotonic() >= deadline:
        raise requests.Timeout("聊天模型响应超过总超时限制")
    payload = _flush_sse_data(lines)
    if payload:
        yield payload


def _flush_sse_data(lines: list[str]) -> str | None:
    if not lines:
        return None
    payload = "\n".join(lines).strip()
    lines.clear()
    return payload if payload and payload != "[DONE]" else None


def _response_output_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    output = payload.get("output")
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict) or part.get("type") not in {"output_text", "text"}:
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


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


def _close_response(response: requests.Response) -> None:
    try:
        response.close()
    except Exception:
        pass


def _timeout_error(
    started: float,
    *,
    response: requests.Response | None = None,
    exception_type: str = "ChatDeadlineExceeded",
) -> OpenAIChatError:
    return OpenAIChatError(
        "聊天模型响应超时",
        code="chat_timeout",
        status_code=504,
        request_id=_request_id(response) if response is not None else "",
        upstream_status=response.status_code if response is not None else None,
        elapsed_seconds=_elapsed(started),
        details={"exception_type": exception_type},
    )
