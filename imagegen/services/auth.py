from __future__ import annotations

import time
from threading import Lock

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import func, select

from ..errors import ServiceError
from ..extensions import db
from ..models import User, utcnow

MIN_PASSWORD_LENGTH = 10
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60
LOGIN_FAILURE_KEY_LIMIT = 4096


class AuthService:
    def __init__(self):
        self._passwords = PasswordHasher()
        self._login_failure_lock = Lock()
        self._login_failures: dict[tuple[str, str], list[float]] = {}

    def authenticate(self, username: str, password: str, *, client_id: str = "") -> User | None:
        normalized = username.strip().lower()
        failure_key = (client_id.strip(), normalized)
        self._raise_if_login_limited(failure_key)
        user = db.session.scalar(select(User).where(func.lower(User.username) == normalized))
        if user is None or not user.is_active:
            self._record_login_failure(failure_key)
            return None
        try:
            self._passwords.verify(user.password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            self._record_login_failure(failure_key)
            return None
        self._clear_login_failures(failure_key)
        if self._passwords.check_needs_rehash(user.password_hash):
            user.password_hash = self._passwords.hash(password)
        user.last_login_at = utcnow()
        db.session.commit()
        return user

    def set_password(self, user: User, password: str) -> None:
        if not password:
            raise ServiceError("密码不能为空")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ServiceError(f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符")
        if len(password) > 200:
            raise ServiceError("密码不能超过 200 个字符")
        user.password_hash = self._passwords.hash(password)
        user.password_version = (user.password_version or 0) + 1

    def verify_password(self, user: User, password: str) -> bool:
        try:
            return self._passwords.verify(user.password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False

    def _raise_if_login_limited(self, key: tuple[str, str]) -> None:
        now = time.monotonic()
        with self._login_failure_lock:
            failures = self._recent_login_failures(key, now)
            if len(failures) >= LOGIN_FAILURE_LIMIT:
                raise ServiceError(
                    "登录失败次数过多，请 5 分钟后重试",
                    code="login_rate_limited",
                    status_code=429,
                )

    def _record_login_failure(self, key: tuple[str, str]) -> None:
        now = time.monotonic()
        with self._login_failure_lock:
            failures = self._recent_login_failures(key, now)
            failures.append(now)
            self._login_failures[key] = failures
            while len(self._login_failures) > LOGIN_FAILURE_KEY_LIMIT:
                self._login_failures.pop(next(iter(self._login_failures)))

    def _clear_login_failures(self, key: tuple[str, str]) -> None:
        with self._login_failure_lock:
            self._login_failures.pop(key, None)

    def _recent_login_failures(self, key: tuple[str, str], now: float) -> list[float]:
        cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
        failures = [attempt for attempt in self._login_failures.get(key, ()) if attempt > cutoff]
        self._login_failures.pop(key, None)
        if failures:
            self._login_failures[key] = failures
        return failures
