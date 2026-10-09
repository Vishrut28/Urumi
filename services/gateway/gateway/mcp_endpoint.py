"""The gateway's own MCP endpoint: one URL, every enabled upstream's tools.

Request path:
  client --Bearer API key--> McpAuth (who is this?) --> tools/list | tools/call
  tools/call: resolve `<slug>__<tool>` -> permission -> rate limit -> upstream
  session with the server-side credential -> result back -> audit row.

The client's own credential stops here. Upstreams only ever see the gateway's
static credential for them, never the caller's API key.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any

import mcp_types as types
from mcp.server import Server
from sqlalchemy import select
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway import audit, metrics
from gateway.audit import Principal
from gateway.config import Settings
from gateway.db import ApiKey, Database, UpstreamServer, User, utcnow
from gateway.security import API_KEY_PREFIX, CredentialBox, hash_api_key, verify_token
from gateway.upstreams import SEP, HealthMonitor, UpstreamFailure, as_gateway_tool, call_tool, guard, target_for

log = logging.getLogger("gateway.mcp")

INSTRUCTIONS = (
    "Team MCP gateway. Every tool is named <server>__<tool>; the prefix says which system it acts on "
    "(for example woocommerce__list_orders). Tool descriptions start with the server's display name."
)


class Gateway:
    def __init__(self, settings: Settings, db: Database, box: CredentialBox, monitor: HealthMonitor):
        self.settings = settings
        self.db = db
        self.box = box
        self.monitor = monitor
        self.allowed_internal = [p.strip() for p in settings.ssrf_allowed_internal_hosts.split(",")]
        self.server = Server("team-mcp-gateway", version="1.0.0", instructions=INSTRUCTIONS, on_list_tools=self._list_tools, on_call_tool=self._call_tool)

    # --- tools visible to a principal ---------------------------------------

    async def visible_tools(self, principal: Principal) -> list[tuple[UpstreamServer, dict[str, Any]]]:
        async with self.db.session() as s:
            servers = (
                await s.scalars(select(UpstreamServer).where(UpstreamServer.enabled.is_(True), UpstreamServer.status == "connected").order_by(UpstreamServer.id))
            ).all()
        out = []
        for server in servers:
            for entry in server.tools or []:
                if self.allowed(principal, server, entry["upstream"]):
                    out.append((server, entry))
        return out

    @staticmethod
    def allowed(principal: Principal, server: UpstreamServer, upstream_tool: str) -> bool:
        return principal.is_admin or upstream_tool not in (server.admin_only_tools or [])

    async def _list_tools(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        principal = _principal(ctx)
        return types.ListToolsResult(tools=[as_gateway_tool(server.slug, server.name, entry) for server, entry in await self.visible_tools(principal)])

    # --- calls ----------------------------------------------------------------

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await self.invoke(_principal(ctx), params.name, params.arguments or {})

    async def invoke(self, principal: Principal, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        started = time.monotonic()
        slug = name.split(SEP, 1)[0] if SEP in name else None

        async with self.db.session() as s:
            server = await s.scalar(select(UpstreamServer).where(UpstreamServer.slug == slug)) if slug else None
        entry = next((e for e in (server.tools or []) if e["name"] == name), None) if server else None

        if server is None or not server.enabled or entry is None:
            await audit.record(self.db, principal, tool=name, server=slug if server else None, started=started, outcome="error", error="unknown or disabled tool")
            return _error(f"Unknown tool {name!r}. It does not exist or its server is disabled; list tools again.")
        tool = entry["upstream"]
        if not self.allowed(principal, server, tool):
            await audit.record(self.db, principal, tool=tool, server=slug, started=started, outcome="denied", error="admin-only tool")
            return _error(f"Permission denied: {name} is restricted to admins.")
        limit = self.settings.rate_limit_calls_per_minute
        if limit and await audit.calls_last_minute(self.db, principal.user_id) >= limit:
            await audit.record(self.db, principal, tool=tool, server=slug, started=started, outcome="rejected", error=f"rate limit {limit}/min")
            return _error(f"Rate limit exceeded: at most {limit} tool calls per minute. Try again shortly.")

        target = target_for(server, self.box)
        try:
            await guard(target, self.allowed_internal, self.settings.ssrf_allow_all)
            result = await call_tool(target, tool, arguments)
        except UpstreamFailure as exc:
            if exc.outcome in ("unreachable", "timeout"):
                await self.monitor.mark_unreachable(server.id, exc.reason)
            await audit.record(self.db, principal, tool=tool, server=slug, started=started, outcome=exc.outcome, error=exc.reason)
            return _error(f"Upstream '{server.name}' failed: {exc.reason}. Other servers are unaffected.")

        error_text = None
        if result.is_error:
            error_text = " ".join(c.text for c in result.content if isinstance(c, types.TextContent))[:500] or "tool reported an error"
        await audit.record(self.db, principal, tool=tool, server=slug, started=started, outcome="error" if result.is_error else "ok", error=error_text)
        return types.CallToolResult(content=result.content, structured_content=result.structured_content, is_error=result.is_error)


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=True)


def _principal(ctx: Any) -> Principal:
    request = getattr(ctx, "request", None)
    principal = getattr(getattr(request, "state", None), "principal", None) if request is not None else None
    if not isinstance(principal, Principal):  # McpAuth guarantees this; refuse rather than run anonymously
        raise PermissionError("unauthenticated")
    return principal


class McpAuth:
    """Authenticates every request to /mcp with a gateway API key (or a chat-agent token)."""

    def __init__(self, app: ASGIApp, gateway: Gateway):
        self.app = app
        self.gw = gateway

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        token = headers.get("x-api-key") or ""
        auth = headers.get("authorization", "")
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            metrics.AUTH_FAILURES.labels("missing").inc()
            return await _deny(send, "missing API key: send 'Authorization: Bearer <key>'")

        principal: Principal | None = None
        settings = self.gw.settings
        if token.startswith("c."):
            payload = verify_token(token, "c", settings.session_secret)
            if payload:
                async with self.gw.db.session() as s:
                    user = await s.get(User, int(payload["uid"]))
                if user and user.active:
                    principal = Principal(user.id, user.email, user.role, "chat")
        elif token.startswith(API_KEY_PREFIX):
            async with self.gw.db.session() as s:
                row = (await s.execute(select(ApiKey, User).join(User).where(ApiKey.key_hash == hash_api_key(token, settings.api_key_pepper)))).first()
                if row:
                    key, user = row
                    if key.revoked_at is not None or not user.active:
                        metrics.AUTH_FAILURES.labels("revoked").inc()
                        reason = "API key revoked" if key.revoked_at is not None else "user deactivated"
                        await self._audit_rejected(receive, user, key.prefix, reason)
                        return await _deny(send, f"{reason}: generate a new key in the dashboard")
                    principal = Principal(user.id, user.email, user.role, "mcp", key.prefix)
                    if key.last_used_at is None or utcnow() - key.last_used_at > timedelta(seconds=60):
                        key.last_used_at = utcnow()
                        await s.commit()
        if principal is None:
            metrics.AUTH_FAILURES.labels("invalid").inc()
            return await _deny(send, "invalid API key")
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)

    async def _audit_rejected(self, receive: Receive, user: User, prefix: str, reason: str) -> None:
        """A revoked key's tool call is still attributable: record it, then reject."""
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        try:
            msg = json.loads(body or b"{}")
        except ValueError:
            return
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            name = str((msg.get("params") or {}).get("name", "?"))
            slug = name.split(SEP, 1)[0] if SEP in name else None
            await audit.record(self.gw.db, None, tool=name.split(SEP, 1)[-1], server=slug, started=time.monotonic(), outcome="rejected", error=reason, source="mcp", user_id=user.id, user_email=user.email, key_prefix=prefix)


async def _deny(send: Send, message: str) -> None:
    body = json.dumps({"error": "unauthorized", "message": message}).encode()
    await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b'Bearer realm="mcp-gateway"')]})
    await send({"type": "http.response.body", "body": body})
