from __future__ import annotations

import copy
import threading
import weakref
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import requests

from ..config.matting_models import normalize_adapter_id
from ..errors import ServiceError
from .chroma import ChromaKeyAdapter, TwoPassChromaKeyAdapter
from .matting import GenericMattingClient, LucidaMattingClient


class BackgroundRemovalAdapter(Protocol):
    """Common contract used by the worker and health checks."""

    adapter_id: str

    def remove_background(self, content: bytes, *, filename: str = "image.png") -> bytes:
        ...

    def healthcheck(self) -> None:
        ...

    def close(self) -> None:
        ...


class MattingAdapterFactory:
    """Build adapters from immutable model/result snapshots.

    The factory owns reusable HTTP sessions. Local processors are stateless and
    are created per task so a queued result can safely keep its own options.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
        lucida_client_cls: Callable[..., BackgroundRemovalAdapter] = LucidaMattingClient,
    ) -> None:
        self._session_factory = session_factory
        self._lucida_client_cls = lucida_client_cls
        # Session instances are isolated per worker thread.  A single factory
        # is shared by web health checks and background tasks, but requests'
        # Session object is not guaranteed to be thread-safe.
        self._sessions: weakref.WeakKeyDictionary[
            threading.Thread, dict[str, requests.Session]
        ] = weakref.WeakKeyDictionary()
        self._lock = threading.RLock()

    def create(
        self,
        source: Any,
        *,
        lucida_client_cls: Callable[..., BackgroundRemovalAdapter] | None = None,
    ) -> BackgroundRemovalAdapter:
        adapter_id = self._source_value(source, "adapter_id", "lucida") or "lucida"
        try:
            adapter_id = normalize_adapter_id(adapter_id)
        except ValueError as exc:
            raise ServiceError(str(exc), code="matting_adapter_unsupported", status_code=500) from exc
        base_url = str(self._source_value(source, "base_url", "") or "").strip()
        model = str(self._source_value(source, "model", "") or "").strip()
        timeout = float(self._source_value(source, "timeout_seconds", 120) or 120)
        options = self._source_value(source, "options", None)
        if options is None:
            options = self._source_value(source, "adapter_options", {})
        if not isinstance(options, Mapping):
            options = {}
        options = copy.deepcopy(dict(options))

        if adapter_id == "lucida":
            # ``lucida_client_cls`` remains as a compatibility injection hook
            # for callers that used the old factory API.  Production callers
            # inject the transport once through the constructor.
            client_cls = lucida_client_cls or self._lucida_client_cls
            return client_cls(
                base_url=base_url,
                model=model or "lucida",
                timeout_seconds=timeout,
                decontaminate=bool(options.get("decontaminate", True)),
                session=self._session_for(base_url),
            )
        if adapter_id == "generic_http":
            return GenericMattingClient(
                base_url=base_url,
                model=model,
                timeout_seconds=timeout,
                remove_path=str(options.get("remove_path", "/remove")),
                health_path=str(options.get("health_path", "/health")),
                model_param=str(options.get("model_param", "model")),
                file_field=str(options.get("file_field", "file")),
                response_field=str(options.get("response_field", "")),
                decontaminate=bool(options.get("decontaminate", False)),
                session=self._session_for(base_url),
            )
        if adapter_id == "chroma_key":
            return ChromaKeyAdapter(options, model=model or "balanced")
        if adapter_id == "two_pass_chroma":
            return TwoPassChromaKeyAdapter(options, model=model or "balanced")
        # normalize_adapter_id currently makes this unreachable, but keeping a
        # stable error protects callers constructing malformed snapshots.
        raise ServiceError(
            f"不支持的透明化适配器：{adapter_id}",
            code="matting_adapter_unsupported",
            status_code=500,
        )

    def create_for_result(
        self,
        result: Any,
        *,
        lucida_client_cls: Callable[..., BackgroundRemovalAdapter] | None = None,
    ) -> BackgroundRemovalAdapter:
        return self.create(result, lucida_client_cls=lucida_client_cls)

    def healthcheck(
        self,
        source: Any,
        *,
        lucida_client_cls: Callable[..., BackgroundRemovalAdapter] | None = None,
    ) -> None:
        adapter = self.create(source, lucida_client_cls=lucida_client_cls)
        adapter.healthcheck()

    def close(self) -> None:
        with self._lock:
            sessions = tuple(
                session
                for thread_sessions in self._sessions.values()
                for session in thread_sessions.values()
            )
            self._sessions.clear()
        for session in sessions:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    def _session_for(self, base_url: str) -> requests.Session | None:
        if not base_url:
            return None
        thread = threading.current_thread()
        with self._lock:
            thread_sessions = self._sessions.setdefault(thread, {})
            session = thread_sessions.get(base_url)
            if session is None:
                session = self._session_factory()
                thread_sessions[base_url] = session
            return session

    @staticmethod
    def _source_value(source: Any, name: str, default: Any) -> Any:
        if isinstance(source, Mapping):
            if name in source:
                return source[name]
            aliases = {
                "base_url": "model_base_url",
                "model": "upstream_model",
                "timeout_seconds": "model_timeout_seconds",
                "options": "adapter_options",
            }
            return source.get(aliases.get(name, name), default)
        value = getattr(source, name, None)
        if value is not None:
            return value
        aliases = {
            "base_url": "model_base_url",
            "model": "upstream_model",
            "timeout_seconds": "model_timeout_seconds",
            "options": "adapter_options",
        }
        return getattr(source, aliases.get(name, name), default)


# Short aliases make the integration boundary discoverable to callers and tests.
BackgroundRemovalAdapterFactory = MattingAdapterFactory
LucidaHttpAdapter = LucidaMattingClient
GenericHttpAdapter = GenericMattingClient
MattingAdapter = BackgroundRemovalAdapter

__all__ = [
    "BackgroundRemovalAdapter",
    "BackgroundRemovalAdapterFactory",
    "GenericHttpAdapter",
    "LucidaHttpAdapter",
    "MattingAdapterFactory",
    "MattingAdapter",
]
