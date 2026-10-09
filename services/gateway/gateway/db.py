"""Database models. Postgres in the cluster, SQLite in tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC on the way out, whatever the backend (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.astimezone(UTC) if value is not None and value.tzinfo else value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime(), dict[str, Any]: JSON, list[Any]: JSON}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(16))  # admin | member
    password_hash: Mapped[str | None] = mapped_column(String(400), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    keys: Mapped[list[ApiKey]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16))  # first characters, for recognising a key in the UI
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # HMAC-SHA256(pepper, key)
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    user: Mapped[User] = relationship(back_populates="keys")


class UpstreamServer(Base):
    __tablename__ = "upstream_servers"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(24), unique=True)  # tool-name prefix
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(2000))
    auth_type: Mapped[str] = mapped_column(String(16), default="none")  # none | bearer | header
    auth_header: Mapped[str | None] = mapped_column(String(100), nullable=True)  # header name for auth_type=header
    credential_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet ciphertext, never sent out
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=20.0)
    admin_only_tools: Mapped[list[Any]] = mapped_column(default=list)  # upstream tool names members may not use
    managed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)  # "seed" or "store:<id>"
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Health, written by every replica's probe loop.
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # unknown | connected | unreachable
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    tools: Mapped[list[Any]] = mapped_column(default=list)  # cached tools/list result
    tools_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    source: Mapped[str] = mapped_column(String(16))  # mcp | chat
    api_key_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    server_slug: Mapped[str | None] = mapped_column(String(24), nullable=True)
    tool: Mapped[str] = mapped_column(String(200))  # upstream tool name (or the requested name if unresolved)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str] = mapped_column(String(16))  # ok | error | timeout | unreachable | denied | rejected
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_audit_user_ts", "user_id", "ts"),)


class Store(Base):
    """A provisioned WooCommerce store (bonus: multi-store provisioning)."""

    __tablename__ = "stores"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="provisioning")  # provisioning | ready | failed | deleting
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(String(500))
    namespace: Mapped[str] = mapped_column(String(63))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deadline_at: Mapped[datetime]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=func.now())


class Database:
    def __init__(self, url: str):
        kwargs: dict[str, Any] = {}
        if url.startswith("postgresql"):
            kwargs.update(pool_size=10, max_overflow=10, pool_pre_ping=True)
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                # Replicas start together; serialise schema creation.
                await conn.exec_driver_sql("SELECT pg_advisory_xact_lock(727274)")
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
