from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import socket
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

from ..config.channels import Channel
from ..image_payloads import prepare_image_bytes, prepared_filename
from .diagnostics import response_summary

MAX_OUTPUT_BYTES = 50 * 1024 * 1024


class PinnedHostSSLAdapter(HTTPAdapter):
    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(
            request,
            verify,
            cert,
        )
        host_header = request.headers.get("Host", "")
        if host_header:
            hostname = _host_header_hostname(host_header)
            pool_kwargs["assert_hostname"] = hostname
            pool_kwargs["server_hostname"] = hostname
        return host_params, pool_kwargs


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        status_code: int | None = None,
        request_id: str = "",
        details: dict[str, Any] | None = None,
        provider_completed: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.details = details or {}
        self.provider_completed = provider_completed


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
    idempotency_key: str = ""


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
        headers = {"Authorization": f"Bearer {channel.api_key}"}
        if request.idempotency_key:
            headers["Idempotency-Key"] = request.idempotency_key
        request_data: dict[str, Any]
        if request.references:
            references: list[ReferencePayload] = []
            seen_hashes: set[str] = set()
            for reference in request.references:
                source_hash = hashlib.sha256(reference.content).hexdigest()
                if source_hash in seen_hashes:
                    continue
                content, mime_type = prepare_image_bytes(reference.content, reference.mime_type)
                prepared_hash = hashlib.sha256(content).hexdigest()
                if prepared_hash in seen_hashes:
                    continue
                seen_hashes.update((source_hash, prepared_hash))
                references.append(
                    ReferencePayload(
                        filename=prepared_filename(reference.filename, mime_type),
                        content=content,
                        mime_type=mime_type,
                    )
                )
            request_data = {
                "data": {key: str(value) for key, value in payload.items()},
                "files": [
                    (
                        "image[]",
                        (reference.filename, reference.content, reference.mime_type),
                    )
                    for reference in references
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
            raise ProviderError(
                "渠道请求超时，请稍后重试",
                code="timeout",
                details={"exception_type": exc.__class__.__name__},
            ) from exc
        except requests.RequestException as exc:
            raise ProviderError(
                "无法连接生图渠道，请稍后重试",
                code="connection_error",
                details={"exception_type": exc.__class__.__name__},
            ) from exc

        try:
            request_id = _request_id(response)
            if not 200 <= response.status_code < 300:
                raise ProviderError(
                    _upstream_error(response),
                    code="upstream_error",
                    status_code=response.status_code,
                    request_id=request_id,
                    details=response_summary(response),
                )
            provider_completed = True
            try:
                response_payload = response.json()
            except ValueError as exc:
                raise ProviderError(
                    "上游返回了无效 JSON",
                    code="invalid_response",
                    status_code=response.status_code,
                    request_id=request_id,
                    details=response_summary(response),
                    provider_completed=provider_completed,
                ) from exc
            diagnostics = response_summary(response, response_payload)
        finally:
            response.close()
        content = self._extract(
            response_payload,
            channel,
            request_id,
            diagnostics,
        )

        if len(content) > MAX_OUTPUT_BYTES:
            raise ProviderError(
                "生成图片超过 50 MiB 限制",
                code="output_too_large",
                request_id=request_id,
                provider_completed=True,
            )
        return ProviderResult(content=content, request_id=request_id)

    def _extract(
        self,
        payload: Any,
        channel: Channel,
        request_id: str,
        diagnostics: dict[str, Any],
    ) -> bytes:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderError(
                "上游响应缺少图片数据",
                code="invalid_response",
                request_id=request_id,
                details=diagnostics,
                provider_completed=True,
            )
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            encoded = item.get("b64_json")
            if isinstance(encoded, str) and encoded:
                try:
                    return base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ProviderError(
                        "上游返回了无效图片编码",
                        code="invalid_response",
                        request_id=request_id,
                        details=diagnostics,
                        provider_completed=True,
                    ) from exc
            image_url = item.get("url")
            if isinstance(image_url, str) and image_url:
                return self._download(image_url, channel, request_id)
        raise ProviderError(
            "上游响应中没有可用图片",
            code="invalid_response",
            request_id=request_id,
            details=diagnostics,
            provider_completed=True,
        )

    def _download(self, image_url: str, channel: Channel, request_id: str) -> bytes:
        current_url = image_url
        channel_origin = _url_origin(channel.base_url)
        for _redirect in range(4):
            try:
                parsed, pinned_url, host_header = _pinned_download_target(current_url)
            except ProviderError as exc:
                raise ProviderError(
                    str(exc),
                    code=exc.code,
                    status_code=exc.status_code,
                    request_id=request_id,
                    details=exc.details,
                    provider_completed=True,
                ) from exc
            headers = {"Host": host_header}
            if _url_origin(parsed) == channel_origin:
                headers["Authorization"] = f"Bearer {channel.api_key}"
            try:
                response = self._session().get(
                    pinned_url,
                    headers=headers,
                    timeout=(15, channel.limits.timeout_seconds),
                    stream=True,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise ProviderError(
                    "下载生成图片失败",
                    code="download_error",
                    request_id=request_id,
                    details={"exception_type": exc.__class__.__name__},
                    provider_completed=True,
                ) from exc
            if response.status_code not in {301, 302, 303, 307, 308}:
                try:
                    if not 200 <= response.status_code < 300:
                        raise ProviderError(
                            f"下载生成图片失败（HTTP {response.status_code}）",
                            code="download_error",
                            status_code=response.status_code,
                            request_id=request_id,
                            provider_completed=True,
                        )
                    chunks: list[bytes] = []
                    total = 0
                    try:
                        for chunk in response.iter_content(64 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > MAX_OUTPUT_BYTES:
                                raise ProviderError(
                                    "生成图片超过 50 MiB 限制",
                                    code="output_too_large",
                                    request_id=request_id,
                                    provider_completed=True,
                                )
                            chunks.append(chunk)
                    except requests.RequestException as exc:
                        raise ProviderError(
                            "下载生成图片失败",
                            code="download_error",
                            request_id=request_id,
                            details={"exception_type": exc.__class__.__name__},
                            provider_completed=True,
                        ) from exc
                    return b"".join(chunks)
                finally:
                    response.close()
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise ProviderError(
                    "图片下载重定向缺少地址",
                    code="download_error",
                    request_id=request_id,
                    provider_completed=True,
                )
            current_url = urljoin(current_url, location)
        else:
            raise ProviderError(
                "图片下载重定向次数过多",
                code="download_error",
                request_id=request_id,
                provider_completed=True,
            )
        raise AssertionError("unreachable")

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.mount("https://", PinnedHostSSLAdapter())
            self._local.session = session
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
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = None

    error_codes: list[str] = []
    if isinstance(payload, dict):
        sources: list[dict[str, Any]] = [payload]
        error = payload.get("error")
        if isinstance(error, dict):
            sources.append(error)
        for source in sources:
            for key in ("code", "type"):
                value = source.get(key)
                if isinstance(value, str):
                    code = value.strip().lower()
                    if code and code not in error_codes:
                        error_codes.append(code)

    code_messages = {
        "insufficient_quota": "渠道配额或余额不足",
        "billing_hard_limit_reached": "渠道配额或余额不足",
        "quota_exceeded": "渠道配额或余额不足",
        "rate_limit_exceeded": "渠道请求过于频繁，请稍后重试",
        "rate_limit_error": "渠道请求过于频繁，请稍后重试",
        "invalid_api_key": "渠道 API Key 无效或已失效",
        "authentication_error": "渠道 API Key 无效或已失效",
        "permission_error": "渠道 API Key 没有调用权限",
        "permission_denied": "渠道 API Key 没有调用权限",
    }
    for code in error_codes:
        if code in code_messages:
            return code_messages[code]

    status_messages = {
        401: "渠道 API Key 无效或已失效",
        403: "渠道 API Key 没有调用权限",
        429: "渠道请求过于频繁，请稍后重试",
        500: "渠道服务暂时异常，请稍后重试",
        502: "渠道服务暂时异常，请稍后重试",
        503: "渠道服务暂时异常，请稍后重试",
        504: "渠道服务暂时异常，请稍后重试",
        524: "渠道网关等待生成超时",
    }
    if response.status_code in status_messages:
        return status_messages[response.status_code]
    return "渠道请求失败，请稍后重试"


def _pinned_download_target(url: str):
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except (UnicodeError, ValueError) as exc:
        raise ProviderError("上游返回了无效图片地址", code="invalid_response") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or username is not None
        or password is not None
    ):
        raise ProviderError("上游返回了无效图片地址", code="invalid_response")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ProviderError("上游返回了无效图片地址", code="invalid_response") from exc
    address = _resolve_public_address(hostname, port or (443 if parsed.scheme == "https" else 80))
    pinned_host = f"[{address}]" if ":" in address else address
    pinned_netloc = f"{pinned_host}:{port}" if port is not None else pinned_host
    host = f"[{hostname}]" if ":" in hostname else hostname
    host_header = f"{host}:{port}" if port is not None else host
    return parsed, parsed._replace(netloc=pinned_netloc, fragment="").geturl(), host_header


def _url_origin(url) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(url) if isinstance(url, str) else url
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        normalized_hostname = hostname.encode("idna").decode("ascii").lower() if hostname else ""
    except (UnicodeError, ValueError):
        return None
    if not normalized_hostname or parsed.scheme not in {"http", "https"}:
        return None
    return parsed.scheme, normalized_hostname, port


def _host_header_hostname(host_header: str) -> str:
    if host_header.startswith("["):
        end = host_header.find("]")
        return host_header[1:end] if end > 1 else host_header
    hostname, separator, port = host_header.rpartition(":")
    return hostname if separator and port.isdigit() else host_header


def _resolve_public_address(hostname: str, port: int) -> str:
    try:
        addresses = list(
            dict.fromkeys(
                item[4][0]
                for item in socket.getaddrinfo(
                    hostname,
                    port,
                    type=socket.SOCK_STREAM,
                )
            )
        )
    except OSError as exc:
        raise ProviderError("无法解析图片下载地址", code="download_error") from exc
    if not addresses:
        raise ProviderError("无法解析图片下载地址", code="download_error")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ProviderError("图片下载地址解析结果无效", code="download_error") from exc
        if not ip.is_global:
            raise ProviderError("图片下载地址指向了非公网地址", code="invalid_response")
    return addresses[0]
