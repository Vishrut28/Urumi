"""Who is calling, and the audit trail of what they called."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from gateway import metrics
from gateway.db import AuditLog, Database, utcnow

log = logging.getLogger("gateway.audit")


@dataclass(frozen=True)
class Principal:
    user_id: int
    email: str
    role: str  # admin | member
    source: str  # mcp (an API key) | chat (the dashboard agent acting as the user)
    key_prefix: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def record(
    db: Database,
    principal: Principal | None,
    *,
    tool: str,
    server: str | None,
    started: float,
    outcome: str,
    error: str | None = None,
    source: str | None = None,
    user_id: int | None = None,
    user_email: str | None = None,
    key_prefix: str | None = None,
) -> None:
    duration_ms = int((time.monotonic() - started) * 1000)
    src = principal.source if principal else (source or "mcp")
    metrics.TOOL_CALLS.labels(server or "-", src, outcome).inc()
    if server and outcome in ("ok", "error", "timeout", "unreachable"):
        metrics.TOOL_SECONDS.labels(server).observe(duration_ms / 1000)
    try:
        async with db.session() as s:
            s.add(
                AuditLog(
                    user_id=principal.user_id if principal else user_id,
                    user_email=principal.email if principal else user_email,
                    source=src,
                    api_key_prefix=principal.key_prefix if principal else key_prefix,
                    server_slug=server,
                    tool=tool[:200],
                    duration_ms=duration_ms,
                    outcome=outcome,
                    error=(error or None) and error[:2000],
                )
            )
            await s.commit()
    except Exception:  # the call already happened; losing the row must not fail it, but must be loud
        log.exception("audit write failed: user=%s tool=%s outcome=%s", principal and principal.email, tool, outcome)


async def calls_last_minute(db: Database, user_id: int) -> int:
    """Calls by this user in the last 60 s, from the audit log - shared by every replica."""
    async with db.session() as s:
        return int(
            await s.scalar(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.user_id == user_id, AuditLog.ts >= utcnow() - timedelta(seconds=60), AuditLog.outcome != "rejected"
                )
            )
            or 0
        )
