from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func, select

from ..errors import ServiceError
from ..extensions import db
from ..models import AuditLog, GenerationItem, GenerationJob, User, WalletLedger, utcnow
from .common import money

SHANGHAI_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class SpendingSummary:
    total_rmb: Decimal = Decimal("0.0000")
    today_rmb: Decimal = Decimal("0.0000")

    @classmethod
    def combine(cls, summaries: Iterable[SpendingSummary]) -> SpendingSummary:
        values = tuple(summaries)
        return cls(
            total_rmb=money(sum((value.total_rmb for value in values), Decimal("0"))),
            today_rmb=money(sum((value.today_rmb for value in values), Decimal("0"))),
        )

    def public_dict(self) -> dict[str, str]:
        return {
            "today_rmb": format(self.today_rmb, ".4f"),
            "total_rmb": format(self.total_rmb, ".4f"),
        }


class BillingService:
    def spending_summary(self, user_id: int, *, now: datetime | None = None) -> SpendingSummary:
        return self.spending_by_user((user_id,), now=now)[user_id]

    def spending_by_user(
        self, user_ids: Iterable[int], *, now: datetime | None = None
    ) -> dict[int, SpendingSummary]:
        identifiers = tuple(dict.fromkeys(int(user_id) for user_id in user_ids))
        if not identifiers:
            return {}

        day_start = _shanghai_day_start_utc(now or utcnow())
        totals = self._charge_totals(identifiers)
        today = self._charge_totals(identifiers, since=day_start)
        return {
            user_id: SpendingSummary(
                total_rmb=totals.get(user_id, money(0)),
                today_rmb=today.get(user_id, money(0)),
            )
            for user_id in identifiers
        }

    @staticmethod
    def _charge_totals(
        user_ids: tuple[int, ...], *, since: datetime | None = None
    ) -> dict[int, Decimal]:
        query = select(WalletLedger.user_id, func.sum(WalletLedger.amount_rmb)).where(
            WalletLedger.entry_type == "generation_charge",
            WalletLedger.user_id.in_(user_ids),
        )
        if since is not None:
            query = query.where(WalletLedger.created_at >= since)
        rows = db.session.execute(query.group_by(WalletLedger.user_id))
        return {user_id: money(-amount) for user_id, amount in rows if amount is not None}

    def adjust(
        self,
        *,
        user_id: int,
        actor_user_id: int,
        amount: Decimal | str,
        operation: str,
        note: str,
    ) -> User:
        amount_value = money(amount)
        if amount_value < 0:
            raise ServiceError("金额不能为负数")
        if operation not in {"add", "subtract", "set"}:
            raise ServiceError("不支持的余额操作")
        note = note.strip()
        if len(note) > 500:
            raise ServiceError("余额调整备注不能超过 500 字")
        user = self.lock_user(user_id)
        old_balance = money(user.balance_rmb)
        if operation == "add":
            new_balance = old_balance + amount_value
        elif operation == "subtract":
            new_balance = old_balance - amount_value
        else:
            new_balance = amount_value
        new_balance = money(new_balance)
        if new_balance < user.reserved_rmb:
            raise ServiceError("调整后余额不能低于正在生成任务的预占金额")
        delta = money(new_balance - old_balance)
        user.balance_rmb = new_balance
        db.session.add(
            WalletLedger(
                user_id=user.id,
                actor_user_id=actor_user_id,
                entry_type=f"admin_{operation}",
                amount_rmb=delta,
                balance_after_rmb=new_balance,
                note=note,
            )
        )
        db.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action="user.balance.adjust",
                target_type="user",
                target_id=str(user.id),
                details={
                    "operation": operation,
                    "amount_rmb": format(amount_value, ".4f"),
                    "note": note,
                },
            )
        )
        db.session.commit()
        return user

    def reserve(self, user: User, amount: Decimal) -> None:
        amount = money(amount)
        if amount < 0:
            raise ServiceError("预占金额不能为负数")
        if money(user.balance_rmb - user.reserved_rmb) < amount:
            raise ServiceError(
                "余额不足，无法提交生成任务",
                code="insufficient_balance",
                status_code=402,
            )
        user.reserved_rmb = money(user.reserved_rmb + amount)

    def release(self, user: User, job: GenerationJob, amount: Decimal) -> None:
        amount = min(money(amount), money(job.reserved_rmb), money(user.reserved_rmb))
        user.reserved_rmb = money(user.reserved_rmb - amount)
        job.reserved_rmb = money(job.reserved_rmb - amount)

    def capture(self, user: User, job: GenerationJob, item: GenerationItem) -> None:
        if item.charged_rmb > 0:
            return
        price = money(job.price_per_image_rmb)
        self.release(user, job, price)
        if user.balance_rmb < price:
            raise ServiceError("结算时余额不足", code="billing_invariant", status_code=500)
        user.balance_rmb = money(user.balance_rmb - price)
        job.charged_rmb = money(job.charged_rmb + price)
        item.charged_rmb = price
        db.session.add(
            WalletLedger(
                user_id=user.id,
                generation_item_id=item.id,
                entry_type="generation_charge",
                amount_rmb=-price,
                balance_after_rmb=user.balance_rmb,
                note=f"生图扣费 · {job.channel_label} · 任务 {job.id}",
            )
        )

    @staticmethod
    def lock_user(user_id: int) -> User:
        user = db.session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            raise ServiceError("用户不存在", status_code=404)
        return user


def _shanghai_day_start_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(SHANGHAI_TIMEZONE)
    return local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
