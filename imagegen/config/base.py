from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Generic, Protocol, TypeVar

import yaml

from .repository import ConfigOverride, canonical_json_bytes


class VersionedSnapshot(Protocol):
    version: str


SnapshotT = TypeVar("SnapshotT", bound=VersionedSnapshot)


class ReloadableConfigRegistry(Generic[SnapshotT]):
    """Maintains an atomically replaceable YAML or database-backed snapshot."""

    READ_ERROR_PREFIX = "无法读取配置"
    LOAD_ERROR_PREFIX = "配置加载失败"
    NOT_LOADED_MESSAGE = "配置尚未加载"

    def __init__(
        self,
        config_path: str | Path,
        override_loader: Callable[[], ConfigOverride | None] | None = None,
    ):
        self._path = Path(config_path).resolve()
        self._override_loader = override_loader
        self._lock = threading.RLock()
        self._snapshot: SnapshotT | None = None
        self._signature_value: tuple | None = None
        self._last_error = ""
        self._source = "file"
        self.reload(force=True)

    @property
    def version(self) -> str:
        with self._lock:
            return self._require_snapshot().version

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def reload_if_changed(self) -> bool:
        try:
            signature = self._current_signature()
        except (OSError, ValueError) as exc:
            with self._lock:
                self._last_error = f"{self.READ_ERROR_PREFIX}：{exc}"
            return False
        with self._lock:
            if signature == self._signature_value:
                return False
        return self.reload()

    def reload(self, *, force: bool = False) -> bool:
        try:
            override = self._override_loader() if self._override_loader else None
            if override:
                raw = override.document
                raw_bytes = canonical_json_bytes(raw)
                signature = ("database", override.revision)
                source = "database"
            else:
                raw_bytes = self._path.read_bytes()
                raw = yaml.safe_load(raw_bytes) or {}
                signature = ("file", *self._file_signature(raw))
                source = "file"
            snapshot = self._parse(raw, raw_bytes)
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            with self._lock:
                self._last_error = str(exc)
                has_snapshot = self._snapshot is not None
            if force and not has_snapshot:
                raise ValueError(f"{self.LOAD_ERROR_PREFIX}：{exc}") from exc
            return False

        with self._lock:
            self._snapshot = snapshot
            self._signature_value = signature
            self._last_error = ""
            self._source = source
        return True

    def validate(self, document: dict[str, Any]) -> SnapshotT:
        return self._parse(document, canonical_json_bytes(document))

    def _parse(self, raw: Any, raw_bytes: bytes) -> SnapshotT:
        raise NotImplementedError

    def _file_signature(self, _raw: dict[str, Any] | None = None) -> tuple:
        stat = self._path.stat()
        return stat.st_mtime_ns, stat.st_size

    def _current_signature(self) -> tuple:
        override = self._override_loader() if self._override_loader else None
        if override:
            return "database", override.revision
        return "file", *self._file_signature()

    def _require_snapshot(self) -> SnapshotT:
        if self._snapshot is None:
            raise RuntimeError(self.NOT_LOADED_MESSAGE)
        return self._snapshot
