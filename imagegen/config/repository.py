from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ..errors import ServiceError
from ..extensions import db
from ..models import AuditLog, SystemState, utcnow

CHANNEL_CONFIG_KEY = "runtime_config.channels.v1"
CHAT_CONFIG_KEY = "runtime_config.chat_models.v1"


@dataclass(frozen=True)
class ConfigOverride:
    document: dict[str, Any]
    revision: str


class SecretCipher:
    """Encrypts provider credentials with a stable deployment secret."""

    def __init__(self, secret: str):
        if not secret:
            raise ValueError("配置加密密钥不能为空")
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise ValueError(
                "已保存的 API Key 无法解密，请确认 CONFIG_ENCRYPTION_KEY 或 SECRET_KEY 未变化"
            ) from exc


class RuntimeConfigRepository:
    """Stores validated runtime configuration as atomic versioned documents."""

    def __init__(self, cipher: SecretCipher):
        self._cipher = cipher

    def load_channels(self) -> ConfigOverride | None:
        return self._load(CHANNEL_CONFIG_KEY, "channels")

    def load_chat_models(self) -> ConfigOverride | None:
        return self._load(CHAT_CONFIG_KEY, "models")

    def channel_revision(self) -> str:
        return self._revision_for(CHANNEL_CONFIG_KEY)

    def chat_revision(self) -> str:
        return self._revision_for(CHAT_CONFIG_KEY)

    def save_channels(
        self,
        document: dict[str, Any],
        *,
        expected_revision: str,
        actor_user_id: int,
    ) -> str:
        return self._save(
            CHANNEL_CONFIG_KEY,
            "channels",
            document,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            audit_action="runtime.channels.update",
        )

    def save_chat_models(
        self,
        document: dict[str, Any],
        *,
        expected_revision: str,
        actor_user_id: int,
    ) -> str:
        return self._save(
            CHAT_CONFIG_KEY,
            "models",
            document,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            audit_action="runtime.chat_models.update",
        )

    def _load(self, key: str, collection_key: str) -> ConfigOverride | None:
        state = db.session.get(SystemState, key)
        if state is None or not state.value:
            return None
        try:
            payload = json.loads(state.value)
            if payload.get("schema") != 1 or not isinstance(payload.get("document"), dict):
                raise ValueError("配置文档格式无效")
            document = copy.deepcopy(payload["document"])
            items = document.get(collection_key)
            if not isinstance(items, list):
                raise ValueError("配置集合格式无效")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("配置条目格式无效")
                encrypted = str(item.pop("api_key_encrypted", ""))
                item["api_key"] = self._cipher.decrypt(encrypted)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"数据库运行配置损坏：{exc}") from exc
        return ConfigOverride(document=document, revision=self._revision(state.value))

    def _save(
        self,
        key: str,
        collection_key: str,
        document: dict[str, Any],
        *,
        expected_revision: str,
        actor_user_id: int,
        audit_action: str,
    ) -> str:
        state = db.session.get(SystemState, key)
        current_value = state.value if state is not None else None
        current_revision = self._revision(current_value) if current_value else ""
        if expected_revision != current_revision:
            self._raise_conflict()

        stored_document = copy.deepcopy(document)
        items = stored_document.get(collection_key)
        if not isinstance(items, list):
            raise ServiceError("配置集合格式无效")
        item_ids: list[str] = []
        for item in items:
            secret = str(item.pop("api_key", ""))
            item["api_key_encrypted"] = self._cipher.encrypt(secret)
            item_ids.append(str(item.get("id", "")))

        serialized = json.dumps(
            {"schema": 1, "document": stored_document},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if current_value is None:
            db.session.add(SystemState(key=key, value=serialized))
            try:
                db.session.flush()
            except IntegrityError as exc:
                db.session.rollback()
                raise self._conflict_error() from exc
        else:
            updated = db.session.execute(
                update(SystemState)
                .where(SystemState.key == key, SystemState.value == current_value)
                .values(value=serialized, updated_at=utcnow())
                .execution_options(synchronize_session=False)
            )
            if updated.rowcount != 1:
                self._raise_conflict()
        revision = self._revision(serialized)
        db.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action=audit_action,
                target_type="runtime_config",
                target_id=key,
                details={"revision": revision, "items": item_ids},
            )
        )
        db.session.commit()
        return revision

    @staticmethod
    def _conflict_error() -> ServiceError:
        return ServiceError(
            "配置已被其他管理员更新，请刷新后重试",
            code="config_conflict",
            status_code=409,
        )

    def _raise_conflict(self) -> None:
        db.session.rollback()
        raise self._conflict_error()

    def _revision_for(self, key: str) -> str:
        state = db.session.get(SystemState, key)
        return self._revision(state.value) if state and state.value else ""

    @staticmethod
    def _revision(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def canonical_json_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
