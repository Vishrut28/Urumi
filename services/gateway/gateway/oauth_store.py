"""OAuth tables and constants, shared by the OAuth routes and the MCP endpoint's token check."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import Base, UTCDateTime, utcnow

ACCESS_PREFIX, REFRESH_PREFIX = "mgo_", "mgr_"
ACCESS_TTL, REFRESH_TTL, CODE_TTL = 3600, 30 * 86400, 300
SCOPE = "mcp"


def resource_metadata_url(public_url: str) -> str:
    return f"{public_url.rstrip('/')}/.well-known/oauth-protected-resource/mcp"


class OAuthClient(Base):
    __tablename__ = "oauth_clients"
    client_id: Mapped[str] = mapped_column(String(500), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    redirect_uris: Mapped[list[Any]] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(8))  # dcr | cimd
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utcnow)


class OAuthCode(Base):
    __tablename__ = "oauth_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    client_id: Mapped[str] = mapped_column(String(500))
    user_id: Mapped[int] = mapped_column(Integer)
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))
    resource: Mapped[str] = mapped_column(String(500))
    expires_at: Mapped[Any] = mapped_column(UTCDateTime())
    used_at: Mapped[Any | None] = mapped_column(UTCDateTime(), nullable=True)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # access | refresh
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family: Mapped[str] = mapped_column(String(32), index=True)  # one per authorization grant
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    client_id: Mapped[str] = mapped_column(String(500))
    client_name: Mapped[str] = mapped_column(String(200))
    resource: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[Any] = mapped_column(UTCDateTime())
    revoked_at: Mapped[Any | None] = mapped_column(UTCDateTime(), nullable=True)
    last_used_at: Mapped[Any | None] = mapped_column(UTCDateTime(), nullable=True)
