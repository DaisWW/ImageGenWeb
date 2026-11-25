from __future__ import annotations

import hashlib
import re
import secrets

from sqlalchemy import select
from sqlalchemy.orm import joinedload, load_only

from ..errors import ServiceError
from ..extensions import db
from ..models import AuditLog, GenerationApiKey, User, Workspace
from .billing import BillingService


class GenerationApiKeyService:
    def __init__(self, billing: BillingService):
        self.billing = billing

    def get_key(self, user_id: int) -> GenerationApiKey | None:
        return db.session.get(GenerationApiKey, user_id)

    def issue_key(self, user_id: int, workspace_id: str) -> str:
        self.billing.lock_user(user_id)
        workspace = db.session.scalar(
            select(Workspace)
            .options(load_only(Workspace.name, Workspace.kind))
            .where(
                Workspace.id == workspace_id,
                Workspace.user_id == user_id,
                Workspace.kind == "image",
            )
        )
        if workspace is None:
            raise ServiceError("工作站不存在", code="workspace_not_found", status_code=404)

        token = f"snow_{secrets.token_urlsafe(32)}"
        key = self.get_key(user_id)
        action = "rotate" if key else "enable"
        if key is None:
            key = GenerationApiKey(user_id=user_id)
            db.session.add(key)
        key.workspace = workspace
        key.token_digest = _token_digest(token)
        _record_key_change(user_id, workspace.id, action)
        db.session.commit()
        return token

    def revoke_key(self, user_id: int) -> None:
        self.billing.lock_user(user_id)
        key = self.get_key(user_id)
        if key is not None:
            _record_key_change(user_id, key.workspace_id, "disable")
            db.session.delete(key)
        db.session.commit()

    def authenticate(self, token: str) -> GenerationApiKey:
        if not re.fullmatch(r"snow_[A-Za-z0-9_-]{43}", token):
            raise _invalid_key()
        key = db.session.scalar(
            select(GenerationApiKey)
            .join(User)
            .options(
                joinedload(GenerationApiKey.workspace, innerjoin=True).load_only(
                    Workspace.name, Workspace.kind
                )
            )
            .where(GenerationApiKey.token_digest == _token_digest(token), User.status == "active")
        )
        if key is None:
            raise _invalid_key()
        return key

    def lock_active_owner(self, key: GenerationApiKey) -> User:
        # Check the key again under the same account lock used for rotation.
        user = self.billing.lock_user(key.user_id)
        active_key = db.session.scalar(
            select(GenerationApiKey.user_id).where(
                GenerationApiKey.user_id == user.id,
                GenerationApiKey.token_digest == key.token_digest,
            )
        )
        if not user.is_active or active_key is None:
            raise _invalid_key()
        return user


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _record_key_change(user_id: int, workspace_id: str, action: str) -> None:
    db.session.add(
        AuditLog(
            actor_user_id=user_id,
            action=f"generation_api.key.{action}",
            target_type="workspace",
            target_id=workspace_id,
        )
    )


def _invalid_key() -> ServiceError:
    return ServiceError("生图 API 密钥无效", code="invalid_api_key", status_code=401)
