from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .extensions import db

MONEY_TYPE = Numeric(14, 4)
JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_public_id() -> str:
    return uuid.uuid4().hex


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(db.String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(db.String(100), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(db.String(255))
    role: Mapped[str] = mapped_column(db.String(20), default="user", index=True)
    status: Mapped[str] = mapped_column(db.String(20), default="active", index=True)
    balance_rmb: Mapped[Decimal] = mapped_column(MONEY_TYPE, default=Decimal("0"))
    reserved_rmb: Mapped[Decimal] = mapped_column(MONEY_TYPE, default=Decimal("0"))
    generation_concurrency: Mapped[int] = mapped_column(default=2)
    password_version: Mapped[int] = mapped_column(default=1)
    last_login_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("balance_rmb >= 0", name="ck_users_balance_non_negative"),
        CheckConstraint("reserved_rmb >= 0", name="ck_users_reserved_non_negative"),
        CheckConstraint("generation_concurrency BETWEEN 1 AND 16", name="ck_users_concurrency"),
        Index("uq_users_username_lower", func.lower(username), unique=True),
    )

    workspaces: Mapped[list[Workspace]] = relationship(back_populates="user")

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def available_balance_rmb(self) -> Decimal:
        return max(Decimal("0"), self.balance_rmb - self.reserved_rmb)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def get_id(self) -> str:
        return f"{self.id}:{self.password_version or 0}"


class Workspace(TimestampMixin, db.Model):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_workspaces_user_name"),
        Index("ix_workspaces_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(db.String(32), primary_key=True, default=new_public_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(db.String(80))
    kind: Mapped[str] = mapped_column(db.String(20), default="image")
    position: Mapped[int] = mapped_column(default=0)
    settings: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON_TYPE), default=dict)

    user: Mapped[User] = relationship(back_populates="workspaces")
    assets: Mapped[list[Asset]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[GenerationJob]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.created_at",
    )
    conversation_state: Mapped[ConversationState | None] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        uselist=False,
    )


class ConversationMessage(db.Model):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conversation_messages_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(db.String(32), primary_key=True, default=new_public_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(db.String(20))
    kind: Mapped[str] = mapped_column(db.String(30), default="message", index=True)
    content: Mapped[str] = mapped_column(db.Text)
    payload: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON_TYPE), default=dict)
    provider_id: Mapped[str] = mapped_column(db.String(64), default="")
    provider_label: Mapped[str] = mapped_column(db.String(100), default="")
    model: Mapped[str] = mapped_column(db.String(150), default="")
    upstream_request_id: Mapped[str] = mapped_column(db.String(255), default="")
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    elapsed_seconds: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    workspace: Mapped[Workspace] = relationship(back_populates="messages")
    attachments: Mapped[list[ConversationAttachment]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="ConversationAttachment.position",
    )


class ConversationAttachment(db.Model):
    __tablename__ = "conversation_attachments"
    __table_args__ = (
        UniqueConstraint("message_id", "position", name="uq_conversation_attachment_position"),
    )

    message_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), primary_key=True
    )
    position: Mapped[int]

    message: Mapped[ConversationMessage] = relationship(back_populates="attachments")
    asset: Mapped[Asset] = relationship()


class ConversationState(db.Model):
    __tablename__ = "conversation_state"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    summary: Mapped[str] = mapped_column(db.Text, default="")
    summary_through_message_id: Mapped[str] = mapped_column(db.String(32), default="")
    estimated_context_tokens: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)

    workspace: Mapped[Workspace] = relationship(back_populates="conversation_state")


class Asset(TimestampMixin, db.Model):
    __tablename__ = "assets"
    __table_args__ = (Index("ix_assets_workspace_active", "workspace_id", "deleted_at"),)

    id: Mapped[str] = mapped_column(db.String(32), primary_key=True, default=new_public_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    original_name: Mapped[str] = mapped_column(db.String(255))
    storage_path: Mapped[str] = mapped_column(db.String(500), unique=True)
    mime_type: Mapped[str] = mapped_column(db.String(50))
    byte_count: Mapped[int]
    width: Mapped[int]
    height: Mapped[int]
    sha256: Mapped[str] = mapped_column(db.String(64), index=True)
    position: Mapped[int] = mapped_column(default=0)
    deleted_at: Mapped[datetime | None]

    workspace: Mapped[Workspace] = relationship(back_populates="assets")


class LibraryImage(TimestampMixin, db.Model):
    __tablename__ = "library_images"
    __table_args__ = (
        UniqueConstraint("user_id", "sha256", name="uq_library_images_user_sha256"),
        Index("ix_library_images_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(db.String(32), primary_key=True, default=new_public_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    original_name: Mapped[str] = mapped_column(db.String(255))
    storage_path: Mapped[str] = mapped_column(db.String(500), unique=True)
    thumbnail_path: Mapped[str | None] = mapped_column(db.String(500))
    mime_type: Mapped[str] = mapped_column(db.String(50))
    byte_count: Mapped[int]
    width: Mapped[int]
    height: Mapped[int]
    sha256: Mapped[str] = mapped_column(db.String(64))


class GenerationJob(TimestampMixin, db.Model):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        Index("ix_generation_jobs_workspace_created", "workspace_id", "created_at"),
        Index("ix_generation_jobs_user_status", "user_id", "status"),
        Index(
            "uq_generation_jobs_workspace_active",
            "workspace_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running', 'canceling')"),
            postgresql_where=text("status IN ('queued', 'running', 'canceling')"),
        ),
    )

    id: Mapped[str] = mapped_column(db.String(32), primary_key=True, default=new_public_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    channel_id: Mapped[str] = mapped_column(db.String(64), index=True)
    channel_label: Mapped[str] = mapped_column(db.String(100))
    channel_config_version: Mapped[str] = mapped_column(db.String(64))
    kind: Mapped[str] = mapped_column(db.String(20), default="image")
    mode: Mapped[str] = mapped_column(db.String(20))
    prompt: Mapped[str] = mapped_column(db.Text)
    model: Mapped[str] = mapped_column(db.String(100))
    size: Mapped[str] = mapped_column(db.String(20))
    quality: Mapped[str] = mapped_column(db.String(20))
    workflow: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON_TYPE), default=dict)
    output_format: Mapped[str] = mapped_column(db.String(20))
    compression: Mapped[int]
    transparent_background: Mapped[bool] = mapped_column(default=False)
    requested_count: Mapped[int]
    price_per_image_rmb: Mapped[Decimal] = mapped_column(MONEY_TYPE)
    reserved_rmb: Mapped[Decimal] = mapped_column(MONEY_TYPE)
    charged_rmb: Mapped[Decimal] = mapped_column(MONEY_TYPE, default=Decimal("0"))
    status: Mapped[str] = mapped_column(db.String(20), default="queued", index=True)
    cancel_requested_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]

    workspace: Mapped[Workspace] = relationship(back_populates="jobs")
    user: Mapped[User] = relationship()
    references: Mapped[list[GenerationReference]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="GenerationReference.position"
    )
    items: Mapped[list[GenerationItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="GenerationItem.position"
    )


class GenerationReference(db.Model):
    __tablename__ = "generation_references"
    __table_args__ = (UniqueConstraint("job_id", "position", name="uq_job_reference_position"),)

    job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), primary_key=True
    )
    position: Mapped[int]

    job: Mapped[GenerationJob] = relationship(back_populates="references")
    asset: Mapped[Asset] = relationship()


class GenerationItem(TimestampMixin, db.Model):
    __tablename__ = "generation_items"
    __table_args__ = (
        UniqueConstraint("job_id", "position", name="uq_generation_item_position"),
        Index("ix_generation_items_queue", "status", "created_at"),
        Index("ix_generation_items_channel_status", "channel_id", "status"),
    )

    id: Mapped[str] = mapped_column(db.String(32), primary_key=True, default=new_public_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[str] = mapped_column(db.String(64), index=True)
    position: Mapped[int]
    status: Mapped[str] = mapped_column(db.String(20), default="queued", index=True)
    cancel_requested_at: Mapped[datetime | None]
    claimed_by: Mapped[str | None] = mapped_column(db.String(100))
    heartbeat_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    estimated_seconds: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    error_code: Mapped[str | None] = mapped_column(db.String(80))
    error_message: Mapped[str | None] = mapped_column(db.String(1000))
    upstream_status: Mapped[int | None]
    upstream_request_id: Mapped[str | None] = mapped_column(db.String(255))
    elapsed_seconds: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    charged_rmb: Mapped[Decimal] = mapped_column(MONEY_TYPE, default=Decimal("0"))
    output_path: Mapped[str | None] = mapped_column(db.String(500), unique=True)
    thumbnail_path: Mapped[str | None] = mapped_column(db.String(500), unique=True)
    output_mime_type: Mapped[str | None] = mapped_column(db.String(50))
    output_byte_count: Mapped[int | None]
    output_width: Mapped[int | None]
    output_height: Mapped[int | None]

    job: Mapped[GenerationJob] = relationship(back_populates="items")
    user: Mapped[User] = relationship()


class WalletLedger(db.Model):
    __tablename__ = "wallet_ledger"
    __table_args__ = (
        Index("ix_wallet_ledger_user_created", "user_id", "created_at"),
        UniqueConstraint("generation_item_id", name="uq_wallet_generation_item"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    generation_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_items.id", ondelete="SET NULL")
    )
    entry_type: Mapped[str] = mapped_column(db.String(40), index=True)
    amount_rmb: Mapped[Decimal] = mapped_column(MONEY_TYPE)
    balance_after_rmb: Mapped[Decimal] = mapped_column(MONEY_TYPE)
    note: Mapped[str] = mapped_column(db.String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    actor: Mapped[User | None] = relationship(foreign_keys=[actor_user_id])


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(db.String(80), index=True)
    target_type: Mapped[str] = mapped_column(db.String(50))
    target_id: Mapped[str] = mapped_column(db.String(100))
    details: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON_TYPE), default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class RuntimeLog(db.Model):
    """结构化运行事件，与管理员操作审计分开保存。"""

    __tablename__ = "runtime_logs"
    __table_args__ = (
        Index("ix_runtime_logs_created", "created_at"),
        Index("ix_runtime_logs_category_status_created", "category", "status", "created_at"),
        Index("ix_runtime_logs_user_created", "user_id", "created_at"),
        Index("ix_runtime_logs_error_code", "error_code"),
    )

    id: Mapped[str] = mapped_column(db.String(32), primary_key=True, default=new_public_id)
    level: Mapped[str] = mapped_column(db.String(12), default="info", index=True)
    category: Mapped[str] = mapped_column(db.String(30), index=True)
    event: Mapped[str] = mapped_column(db.String(80), index=True)
    status: Mapped[str] = mapped_column(db.String(20), index=True)
    source: Mapped[str] = mapped_column(db.String(30), default="web")
    message: Mapped[str] = mapped_column(db.String(1000), default="")
    user_id: Mapped[int | None]
    user_label: Mapped[str] = mapped_column(db.String(120), default="")
    workspace_id: Mapped[str] = mapped_column(db.String(32), default="")
    workspace_label: Mapped[str] = mapped_column(db.String(100), default="")
    job_id: Mapped[str] = mapped_column(db.String(32), default="")
    item_id: Mapped[str] = mapped_column(db.String(32), default="")
    provider_id: Mapped[str] = mapped_column(db.String(64), default="")
    provider_label: Mapped[str] = mapped_column(db.String(100), default="")
    model: Mapped[str] = mapped_column(db.String(150), default="")
    error_code: Mapped[str] = mapped_column(db.String(80), default="")
    http_status: Mapped[int | None]
    upstream_request_id: Mapped[str] = mapped_column(db.String(255), default="")
    elapsed_seconds: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    details: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON_TYPE), default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class SystemState(db.Model):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(db.String(100), primary_key=True)
    value: Mapped[str] = mapped_column(db.Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class GenerationQueueState(db.Model):
    __tablename__ = "generation_queue_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class WorkerState(db.Model):
    __tablename__ = "worker_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[str | None] = mapped_column(db.String(100))
    heartbeat_at: Mapped[datetime | None]
