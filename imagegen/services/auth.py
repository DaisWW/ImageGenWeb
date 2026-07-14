from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import func, select

from ..errors import ServiceError
from ..extensions import db
from ..models import User, utcnow


class AuthService:
    def __init__(self):
        self._passwords = PasswordHasher()

    def authenticate(self, username: str, password: str) -> User | None:
        normalized = username.strip().lower()
        user = db.session.scalar(select(User).where(func.lower(User.username) == normalized))
        if user is None or not user.is_active:
            return None
        try:
            self._passwords.verify(user.password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return None
        if self._passwords.check_needs_rehash(user.password_hash):
            user.password_hash = self._passwords.hash(password)
        user.last_login_at = utcnow()
        db.session.commit()
        return user

    def set_password(self, user: User, password: str) -> None:
        if not password:
            raise ServiceError("密码不能为空")
        if len(password) > 200:
            raise ServiceError("密码不能超过 200 个字符")
        user.password_hash = self._passwords.hash(password)
        user.password_version = (user.password_version or 0) + 1

    def verify_password(self, user: User, password: str) -> bool:
        try:
            return self._passwords.verify(user.password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False
