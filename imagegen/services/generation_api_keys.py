from __future__ import annotations

import hashlib
import re
import secrets
from decimal import Decimal

from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import AuditLog, GenerationApiKey, User
from .billing import BillingService


class GenerationApiKeyService:
    def __init__(self, billing: BillingService):
        self.billing = billing

    def get_key(self, user_id: int) -> GenerationApiKey | None:
        return db.session.get(GenerationApiKey, user_id)

    def issue_key(self, user_id: int) -> str:
        self.billing.lock_user(user_id)
        token = f"snow_{secrets.token_urlsafe(32)}"
        key = self.get_key(user_id)
        action = "rotate" if key else "enable"
        if key is None:
            key = GenerationApiKey(user_id=user_id)
            db.session.add(key)
        key.token_digest = _token_digest(token)
        _record_key_change(user_id, action)
        db.session.commit()
        return token

    def revoke_key(self, user_id: int) -> None:
        self.billing.lock_user(user_id)
        key = self.get_key(user_id)
        if key is not None:
            _record_key_change(user_id, "disable")
            db.session.delete(key)
        db.session.commit()

    def authenticate(self, token: str) -> tuple[GenerationApiKey, Decimal]:
        if not re.fullmatch(r"snow_[A-Za-z0-9_-]{43}", token):
            raise _invalid_key()
        # Read balance scalars so authentication cannot cache an unlocked User entity.
        row = db.session.execute(
            select(GenerationApiKey, User.balance_rmb, User.reserved_rmb)
            .join(User, User.id == GenerationApiKey.user_id)
            .where(GenerationApiKey.token_digest == _token_digest(token), User.status == "active")
        ).one_or_none()
        if row is None:
            raise _invalid_key()
        key, balance, reserved = row
        return key, max(Decimal("0"), balance - reserved)

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


def _record_key_change(user_id: int, action: str) -> None:
    db.session.add(
        AuditLog(
            actor_user_id=user_id,
            action=f"generation_api.key.{action}",
            target_type="user",
            target_id=str(user_id),
        )
    )


def _invalid_key() -> ServiceError:
    return ServiceError("生图 API 密钥无效", code="invalid_api_key", status_code=401)
