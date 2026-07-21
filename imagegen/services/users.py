from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..errors import ServiceError
from ..extensions import db
from ..models import AuditLog, User, WalletLedger
from .auth import AuthService
from .common import money


class UserService:
    def __init__(self, auth: AuthService):
        self.auth = auth

    def create(
        self,
        *,
        username: str,
        password: str,
        display_name: str = "",
        balance_rmb: Decimal | str = Decimal("0"),
        generation_concurrency: int = 2,
        role: str = "user",
        actor_user_id: int | None = None,
        commit: bool = True,
    ) -> User:
        username = username.strip()
        if not 3 <= len(username) <= 64:
            raise ServiceError("用户名长度必须在 3 到 64 个字符")
        if any(character.isspace() or not character.isprintable() for character in username):
            raise ServiceError("用户名不能包含空白或控制字符")
        if role not in {"admin", "user"}:
            raise ServiceError("用户角色无效")
        if not 1 <= generation_concurrency <= 16:
            raise ServiceError("用户并发必须在 1 到 16 之间")
        if db.session.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
            raise ServiceError("用户名已存在", code="username_exists", status_code=409)
        initial_balance = money(balance_rmb)
        if initial_balance < 0:
            raise ServiceError("初始余额不能为负数")
        user = User(
            username=username,
            display_name=display_name.strip()[:100],
            password_hash="",
            role=role,
            status="active",
            balance_rmb=initial_balance,
            reserved_rmb=money(0),
            generation_concurrency=generation_concurrency,
        )
        self.auth.set_password(user, password)
        db.session.add(user)
        try:
            db.session.flush()
        except IntegrityError as exc:
            db.session.rollback()
            raise ServiceError("用户名已存在", code="username_exists", status_code=409) from exc
        if user.balance_rmb > 0:
            db.session.add(
                WalletLedger(
                    user_id=user.id,
                    actor_user_id=actor_user_id,
                    entry_type="initial_balance",
                    amount_rmb=user.balance_rmb,
                    balance_after_rmb=user.balance_rmb,
                    note="管理员开户初始余额",
                )
            )
        db.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action="user.create",
                target_type="user",
                target_id=str(user.id),
                details={"username": user.username, "role": role},
            )
        )
        if commit:
            db.session.commit()
        return user

    def update_status(self, user_id: int, status: str, actor_user_id: int) -> User:
        if status not in {"active", "disabled"}:
            raise ServiceError("用户状态无效")
        user = db.session.get(User, user_id)
        if user is None:
            raise ServiceError("用户不存在", status_code=404)
        if user.id == actor_user_id and status == "disabled":
            raise ServiceError("管理员不能禁用自己")
        user.status = status
        db.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action="user.status.update",
                target_type="user",
                target_id=str(user.id),
                details={"status": status},
            )
        )
        db.session.commit()
        return user

    def update_profile(
        self,
        user_id: int,
        *,
        display_name: str,
        generation_concurrency: int,
        actor_user_id: int,
    ) -> User:
        if not 1 <= generation_concurrency <= 16:
            raise ServiceError("用户并发必须在 1 到 16 之间")
        user = db.session.get(User, user_id)
        if user is None:
            raise ServiceError("用户不存在", status_code=404)
        old = {
            "display_name": user.display_name,
            "generation_concurrency": user.generation_concurrency,
        }
        user.display_name = display_name.strip()[:100]
        user.generation_concurrency = generation_concurrency
        db.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action="user.profile.update",
                target_type="user",
                target_id=str(user.id),
                details={
                    "old": old,
                    "new": {
                        "display_name": user.display_name,
                        "generation_concurrency": user.generation_concurrency,
                    },
                },
            )
        )
        db.session.commit()
        return user

    def reset_password(self, user_id: int, password: str, actor_user_id: int) -> None:
        user = db.session.get(User, user_id)
        if user is None:
            raise ServiceError("用户不存在", status_code=404)
        self.auth.set_password(user, password)
        db.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action="user.password.reset",
                target_type="user",
                target_id=str(user.id),
                details={},
            )
        )
        db.session.commit()
