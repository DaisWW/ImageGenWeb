from __future__ import annotations

import base64
import binascii
import ipaddress
import socket
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from ..config.channels import Channel

MAX_OUTPUT_BYTES = 50 * 1024 * 1024


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        status_code: int | None = None,
        request_id: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id


@dataclass(frozen=True)
class ReferencePayload:
    filename: str
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    model: str
    size: str
    quality: str
    output_format: str
    compression: int
    transparent_background: bool = False
    references: tuple[ReferencePayload, ...] = ()


@dataclass(frozen=True)
class ProviderResult:
    content: bytes
    request_id: str


class OpenAIImagesAdapter:
    def __init__(self):
        self._local = threading.local()

    def generate(self, channel: Channel, request: GenerationRequest) -> ProviderResult:
        endpoint = "edits" if request.references else "generations"
        url = _api_endpoint(channel.base_url, f"images/{endpoint}")
        payload: dict[str, Any] = {
            "model": request.model,
            "prompt": request.prompt,
            "n": 1,
            "size": request.size,
            "quality": request.quality,
            "output_format": request.output_format,
        }
        if request.output_format in {"jpeg", "webp"}:
            payload["output_compression"] = request.compression
        if request.transparent_background:
            payload["background"] = "transparent"
        headers = {"Authorization": f"Bearer {channel.api_key}"}
        request_data: dict[str, Any]
        if request.references:
            request_data = {
                "data": {key: str(value) for key, value in payload.items()},
                "files": [
                    (
                        channel.capabilities.reference_field,
                        (reference.filename, reference.content, reference.mime_type),
                    )
                    for reference in request.references
                ],
            }
        else:
            headers["Content-Type"] = "application/json"
            request_data = {"json": payload}
        try:
            response = self._session().post(
                url,
                headers=headers,
                timeout=(15, channel.limits.timeout_seconds),
                **request_data,
            )
        except requests.Timeout as exc:
            raise ProviderError("上游生成超时", code="timeout") from exc
        except requests.RequestException as exc:
            raise ProviderError(
                f"无法连接生图渠道：{exc.__class__.__name__}", code="connection_error"
            ) from exc

        request_id = _request_id(response)
        if not 200 <= response.status_code < 300:
            raise ProviderError(
                _upstream_error(response),
                code="upstream_error",
                status_code=response.status_code,
                request_id=request_id,
            )
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                "上游返回了无效 JSON", code="invalid_response", request_id=request_id
            ) from exc
        content = self._extract(response_payload, channel, request_id)
        if len(content) > MAX_OUTPUT_BYTES:
            raise ProviderError("生成图片超过 50 MiB 限制", code="output_too_large")
        return ProviderResult(content=content, request_id=request_id)

    def _extract(self, payload: Any, channel: Channel, request_id: str) -> bytes:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderError("上游响应缺少图片数据", code="invalid_response")
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            encoded = item.get("b64_json")
            if isinstance(encoded, str) and encoded:
                try:
                    return base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ProviderError("上游返回了无效图片编码", code="invalid_response") from exc
            image_url = item.get("url")
            if isinstance(image_url, str) and image_url:
                return self._download(image_url, channel, request_id)
        raise ProviderError("上游响应中没有可用图片", code="invalid_response")

    def _download(self, image_url: str, channel: Channel, request_id: str) -> bytes:
        response = None
        current_url = image_url
        channel_host = urlparse(channel.base_url).hostname
        for _redirect in range(4):
            parsed = urlparse(current_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ProviderError("上游返回了无效图片地址", code="invalid_response")
            _reject_private_host(parsed.hostname)
            headers = {}
            if parsed.hostname == channel_host:
                headers["Authorization"] = f"Bearer {channel.api_key}"
            try:
                response = self._session().get(
                    current_url,
                    headers=headers,
                    timeout=(15, channel.limits.timeout_seconds),
                    stream=True,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise ProviderError("下载生成图片失败", code="download_error") from exc
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise ProviderError("图片下载重定向缺少地址", code="download_error")
            current_url = urljoin(current_url, location)
        else:
            raise ProviderError("图片下载重定向次数过多", code="download_error")
        assert response is not None
        if not 200 <= response.status_code < 300:
            raise ProviderError(
                f"下载生成图片失败（HTTP {response.status_code}）",
                code="download_error",
                status_code=response.status_code,
                request_id=request_id,
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_OUTPUT_BYTES:
                raise ProviderError("生成图片超过 50 MiB 限制", code="output_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
        return self._local.session


class ProviderFactory:
    def __init__(self):
        self._openai_images = OpenAIImagesAdapter()

    def for_channel(self, channel: Channel) -> OpenAIImagesAdapter:
        if channel.adapter == "openai_images":
            return self._openai_images
        raise ProviderError(f"不支持的渠道适配器：{channel.adapter}", code="adapter_error")


def _request_id(response: requests.Response) -> str:
    return (
        response.headers.get("x-request-id")
        or response.headers.get("request-id")
        or response.headers.get("cf-ray")
        or ""
    )[:255]


def _api_endpoint(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/{path}" if base.endswith("/v1") else f"{base}/v1/{path}"


def _upstream_error(response: requests.Response) -> str:
    friendly = {
        401: "API Key 无效或已失效",
        403: "API Key 没有调用该模型的权限",
        429: "渠道请求过于频繁或余额不足",
        524: "渠道网关等待生成超时",
    }
    if response.status_code in friendly:
        return friendly[response.status_code]
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return f"渠道错误：{error['message'][:500]}"
        if isinstance(error, str):
            return f"渠道错误：{error[:500]}"
    return f"渠道返回 HTTP {response.status_code}"


def _reject_private_host(hostname: str) -> None:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise ProviderError("无法解析图片下载地址", code="download_error") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ProviderError("图片下载地址指向了非公网地址", code="invalid_response")
