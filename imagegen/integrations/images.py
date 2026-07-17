from __future__ import annotations

import base64
import binascii
import io
import ipaddress
import socket
import threading
import warnings
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image, ImageFilter, UnidentifiedImageError
from requests.adapters import HTTPAdapter

from ..config.channels import Channel
from .diagnostics import response_summary

MAX_OUTPUT_BYTES = 50 * 1024 * 1024
MAX_TRANSPARENCY_PIXELS = 40_000_000
TRANSPARENT_BACKGROUND_SUFFIX = (
    "\n\nTechnical output requirements for transparency: place the requested subject on a "
    "genuinely transparent canvas. Do not draw or simulate a transparency checkerboard. "
    "Preserve a clean, well-defined silhouette with smooth anti-aliased edges and fully "
    "transparent pixels immediately outside it. Avoid unintended matte colors, halos, fringes, "
    "glow, shadows, stray pixels, and semi-transparent specks around the contour."
)
TRANSPARENT_FALLBACK_SUFFIX = (
    "\n\nTechnical output requirement: render the subject on a perfectly uniform pure white "
    "background (#FFFFFF) extending to every canvas edge. Do not draw or simulate a transparency "
    "checkerboard. Keep a clean, well-defined silhouette with clear color separation from the "
    "background. Do not add texture, gradients, halos, fringes, glow, shadows, stray pixels, "
    "specks, borders, scenery, or extra objects around the subject or in the background."
)


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
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.details = details or {}


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
        prompt = request.prompt
        if request.transparent_background:
            prompt = f"{prompt.rstrip()}{TRANSPARENT_BACKGROUND_SUFFIX}"
        payload: dict[str, Any] = {
            "model": request.model,
            "prompt": prompt,
            "n": 1,
            "size": request.size,
            "quality": request.quality,
            "output_format": request.output_format,
        }
        if request.output_format in {"jpeg", "webp"}:
            payload["output_compression"] = request.compression
        if request.transparent_background:
            payload["background"] = "transparent"
        fallback_attempted = False
        while True:
            headers = {"Authorization": f"Bearer {channel.api_key}"}
            request_data: dict[str, Any]
            if request.references:
                request_data = {
                    "data": {key: str(value) for key, value in payload.items()},
                    "files": [
                        (
                            "image[]",
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
                raise ProviderError(
                    "上游生成超时",
                    code="timeout",
                    details={"exception_type": exc.__class__.__name__},
                ) from exc
            except requests.RequestException as exc:
                raise ProviderError(
                    f"无法连接生图渠道：{exc.__class__.__name__}",
                    code="connection_error",
                    details={"exception_type": exc.__class__.__name__},
                ) from exc

            try:
                request_id = _request_id(response)
                if not 200 <= response.status_code < 300:
                    message = _upstream_error(response)
                    if (
                        request.transparent_background
                        and not fallback_attempted
                        and "background" in payload
                        and _transparent_background_unsupported(message)
                    ):
                        payload = {
                            **payload,
                            "prompt": f"{request.prompt.rstrip()}{TRANSPARENT_FALLBACK_SUFFIX}",
                        }
                        payload.pop("background", None)
                        fallback_attempted = True
                        continue
                    raise ProviderError(
                        message,
                        code="upstream_error",
                        status_code=response.status_code,
                        request_id=request_id,
                        details=response_summary(response),
                    )
                try:
                    response_payload = response.json()
                except ValueError as exc:
                    raise ProviderError(
                        "上游返回了无效 JSON",
                        code="invalid_response",
                        status_code=response.status_code,
                        request_id=request_id,
                        details=response_summary(response),
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
            break

        if len(content) > MAX_OUTPUT_BYTES:
            raise ProviderError("生成图片超过 50 MiB 限制", code="output_too_large")
        if request.transparent_background:
            content = _ensure_transparent_image(
                content,
                output_format=request.output_format,
                compression=request.compression,
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
                    ) from exc
            image_url = item.get("url")
            if isinstance(image_url, str) and image_url:
                return self._download(image_url, channel, request_id)
        raise ProviderError(
            "上游响应中没有可用图片",
            code="invalid_response",
            request_id=request_id,
            details=diagnostics,
        )

    def _download(self, image_url: str, channel: Channel, request_id: str) -> bytes:
        current_url = image_url
        channel_origin = _url_origin(channel.base_url)
        for _redirect in range(4):
            parsed, pinned_url, host_header = _pinned_download_target(current_url)
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
                raise ProviderError("下载生成图片失败", code="download_error") from exc
            if response.status_code not in {301, 302, 303, 307, 308}:
                try:
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
                            raise ProviderError(
                                "生成图片超过 50 MiB 限制",
                                code="output_too_large",
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
                finally:
                    response.close()
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise ProviderError("图片下载重定向缺少地址", code="download_error")
            current_url = urljoin(current_url, location)
        else:
            raise ProviderError("图片下载重定向次数过多", code="download_error")
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


def _transparent_background_unsupported(message: str) -> bool:
    normalized = message.lower()
    return (
        "transparent" in normalized
        and "background" in normalized
        and ("not supported" in normalized or "unsupported" in normalized)
    ) or ("透明背景" in message and "不支持" in message)


def _ensure_transparent_image(
    content: bytes,
    *,
    output_format: str,
    compression: int,
) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if width * height > MAX_TRANSPARENCY_PIXELS:
                    raise ProviderError(
                        "生成图片像素数量超过透明背景处理限制",
                        code="output_too_large",
                    )
                image.load()
                rgba = image.convert("RGBA")
    except ProviderError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ProviderError("生成图片像素数量超过安全限制", code="invalid_response") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ProviderError("上游返回的图片无效", code="invalid_response") from exc

    if rgba.getchannel("A").getextrema()[0] < 255:
        return content

    converted = _remove_simple_light_background(rgba.convert("RGB"))
    if converted is None:
        raise ProviderError(
            "上游未返回透明通道，且背景无法可靠转换",
            code="transparent_background_unsupported",
        )

    output = io.BytesIO()
    if output_format == "webp":
        converted.save(output, format="WEBP", quality=compression, method=4)
    else:
        converted.save(output, format="PNG")
    result = output.getvalue()
    if len(result) > MAX_OUTPUT_BYTES:
        raise ProviderError("生成图片超过 50 MiB 限制", code="output_too_large")
    return result


def _remove_simple_light_background(image: Image.Image) -> Image.Image | None:
    width, height = image.size
    total = width * height
    candidate = bytearray(total)
    for index, (red, green, blue) in enumerate(image.get_flattened_data()):
        if min(red, green, blue) >= 224 and max(red, green, blue) - min(red, green, blue) <= 18:
            candidate[index] = 1

    connected = bytearray(total)
    pending: deque[int] = deque()

    def enqueue(index: int) -> None:
        if candidate[index] and not connected[index]:
            connected[index] = 1
            pending.append(index)

    for x in range(width):
        enqueue(x)
        enqueue((height - 1) * width + x)
    for y in range(height):
        enqueue(y * width)
        enqueue(y * width + width - 1)

    while pending:
        index = pending.popleft()
        x = index % width
        y = index // width
        if x:
            enqueue(index - 1)
        if x + 1 < width:
            enqueue(index + 1)
        if y:
            enqueue(index - width)
        if y + 1 < height:
            enqueue(index + width)

    background_ratio = connected.count(1) / total
    if not 0.05 <= background_ratio <= 0.98:
        return None

    background = Image.frombytes(
        "L",
        image.size,
        bytes(255 if value else 0 for value in connected),
    )
    background = background.filter(ImageFilter.MaxFilter(3))
    background = background.filter(ImageFilter.GaussianBlur(0.65))
    alpha = background.point(lambda value: 255 - value)
    result = image.convert("RGBA")
    result.putalpha(alpha)
    return result


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
