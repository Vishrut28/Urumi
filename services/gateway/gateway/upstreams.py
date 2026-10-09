"""Connecting to upstream MCP servers: tool discovery, health, naming and call routing.

Each upstream call opens its own short Streamable HTTP session with a hard
deadline. Nothing is shared between upstreams, so a slow or dead server can only
cost the caller who asked for its tool. Tool lists come from a cache that the
health loop refreshes, so `tools/list` never waits on an upstream at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy import select

from gateway import metrics
from gateway.db import Database, UpstreamServer, utcnow
from gateway.security import CredentialBox
from gateway.ssrf import UnsafeURL, check_url

log = logging.getLogger("gateway.upstreams")

SEP = "__"
NAME_MAX = 64  # OpenAI- and Gemini-style function names: [A-Za-z0-9_-]{1,64}
DESCRIPTION_MAX = 1024
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,22}[a-z0-9]$|^[a-z]$")


class UpstreamFailure(Exception):
    """An upstream could not serve a request. `outcome` is what the audit log records."""

    def __init__(self, outcome: str, reason: str):
        super().__init__(reason)
        self.outcome = outcome  # timeout | unreachable | error | blocked
        self.reason = reason


@dataclass
class Target:
    id: int
    slug: str
    url: str
    timeout: float
    headers: dict[str, str] = field(default_factory=dict, repr=False)


def target_for(server: UpstreamServer, box: CredentialBox) -> Target:
    headers: dict[str, str] = {}
    if server.auth_type != "none" and server.credential_enc:
        secret = box.open(server.credential_enc)
        if server.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif server.auth_type == "header" and server.auth_header:
            headers[server.auth_header] = secret
    return Target(server.id, server.slug, server.url, server.timeout_seconds, headers)


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for sub in exc.exceptions:
            out.extend(_leaf_exceptions(sub))
        return out
    return [exc]


def classify(exc: BaseException, last_status: int | None, timeout: float) -> UpstreamFailure:
    """Turn whatever the transport raised into one clear, credential-free reason."""
    for leaf in _leaf_exceptions(exc):
        if isinstance(leaf, UpstreamFailure):
            return leaf
        if isinstance(leaf, (TimeoutError, httpx2.TimeoutException)):
            return UpstreamFailure("timeout", f"no response within {timeout:g}s")
        if isinstance(leaf, httpx2.ConnectError):
            text = str(leaf).lower()
            if "getaddrinfo" in text or "name or service" in text or "nodename" in text or "name resolution" in text:
                return UpstreamFailure("unreachable", "host name does not resolve")
            return UpstreamFailure("unreachable", "connection refused or no route to host")
        if isinstance(leaf, httpx2.HTTPError):
            return UpstreamFailure("unreachable", f"transport error ({type(leaf).__name__})")
        if isinstance(leaf, MCPError) and last_status and last_status >= 400:
            hint = {401: " - check the credential", 403: " - credential not permitted", 404: " - check the URL path"}.get(last_status, "")
            return UpstreamFailure("unreachable", f"upstream answered HTTP {last_status}{hint}")
        if isinstance(leaf, MCPError):
            return UpstreamFailure("error", f"upstream protocol error: {leaf.error.message}")
    first = _leaf_exceptions(exc)[0]
    return UpstreamFailure("error", f"unexpected {type(first).__name__}")


@asynccontextmanager
async def open_session(target: Target, timeout: float) -> AsyncIterator[Client]:
    status: dict[str, int] = {}

    async def remember_status(response: httpx2.Response) -> None:
        status["last"] = response.status_code

    http = httpx2.AsyncClient(
        headers=target.headers,
        timeout=httpx2.Timeout(timeout),
        event_hooks={"response": [remember_status]},
    )
    try:
        async with http, Client(streamable_http_client(target.url, http_client=http, terminate_on_close=False)) as client:
            yield client
    except UpstreamFailure:
        raise
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)) and not isinstance(exc, TimeoutError):
            raise
        raise classify(exc, status.get("last"), timeout) from None


async def guard(target: Target, allowed_internal: list[str], allow_all: bool) -> None:
    try:
        await check_url(target.url, allowed_internal, allow_all)
    except UnsafeURL as exc:
        raise UpstreamFailure("blocked", f"URL refused by SSRF guard: {exc}") from None


async def fetch_tools(target: Target, timeout: float) -> list[types.Tool]:
    tools: list[types.Tool] = []
    try:
        with anyio.fail_after(timeout):
            async with open_session(target, timeout) as client:
                cursor: str | None = None
                for _ in range(20):  # pagination cap
                    page = await client.list_tools(cursor=cursor) if cursor else await client.list_tools()
                    tools.extend(page.tools)
                    cursor = page.next_cursor
                    if not cursor:
                        break
    except TimeoutError:
        raise UpstreamFailure("timeout", f"no response within {timeout:g}s") from None
    return tools


async def call_tool(target: Target, tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
    try:
        with anyio.fail_after(target.timeout):
            async with open_session(target, target.timeout) as client:
                return await client.call_tool(tool, arguments)
    except TimeoutError:
        raise UpstreamFailure("timeout", f"no response within {target.timeout:g}s") from None


# --- naming -----------------------------------------------------------------


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "tool"


def expose(slug: str, tools: list[types.Tool]) -> list[dict[str, Any]]:
    """Cache entries for one server: the gateway-facing name, the upstream name and the definition.

    Scheme: `<slug>__<tool>`, always. Prefixing every tool (not only colliding
    ones) keeps names stable when another server is added later, tells the model
    which system a tool acts on, and makes routing a string split. Characters
    outside [A-Za-z0-9_-] become `_`, names are cut to 64 characters, and any
    clash that this creates inside one server gets a numeric suffix.
    """
    entries: list[dict[str, Any]] = []
    used: set[str] = set()
    for tool in tools:
        base = f"{slug}{SEP}{_sanitize(tool.name)}"[:NAME_MAX]
        name, n = base, 2
        while name in used:
            suffix = f"_{n}"
            name, n = base[: NAME_MAX - len(suffix)] + suffix, n + 1
        used.add(name)
        definition = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        entries.append({"name": name, "upstream": tool.name, "definition": definition})
    return entries


def tools_hash(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps([e["definition"] for e in entries], sort_keys=True).encode()).hexdigest()


def as_gateway_tool(slug: str, server_name: str, entry: dict[str, Any]) -> types.Tool:
    d = dict(entry["definition"])
    description = (d.get("description") or "").strip()
    if len(description) > DESCRIPTION_MAX:
        description = description[:DESCRIPTION_MAX] + "..."
    d["name"] = entry["name"]
    d["description"] = f"[{server_name}] {description}".strip()
    d.pop("_meta", None)
    d.pop("icons", None)
    return types.Tool.model_validate(d)


# --- health -----------------------------------------------------------------


class HealthMonitor:
    """Probes every enabled upstream on an interval and caches its tools in the database."""

    def __init__(self, db: Database, box: CredentialBox, interval: float, timeout: float, allowed_internal: list[str], allow_all: bool):
        self.db = db
        self.box = box
        self.interval = interval
        self.timeout = timeout
        self.allowed_internal = allowed_internal
        self.allow_all = allow_all
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="upstream-health")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.probe_all()
            except Exception:
                log.exception("health sweep failed")
            await asyncio.sleep(max(1.0, self.interval - (time.monotonic() - started)))

    async def probe_all(self) -> None:
        async with self.db.session() as s:
            ids = (await s.scalars(select(UpstreamServer.id).where(UpstreamServer.enabled.is_(True)))).all()
        await asyncio.gather(*(self.probe(i) for i in ids))

    async def probe(self, server_id: int) -> None:
        async with self.db.session() as s:
            server = await s.get(UpstreamServer, server_id)
            if server is None or not server.enabled:
                return
            target = target_for(server, self.box)
            slug = server.slug
        started = time.monotonic()
        try:
            await guard(target, self.allowed_internal, self.allow_all)
            tools = await fetch_tools(target, self.timeout)
            entries = expose(slug, tools)
            update: dict[str, Any] = {"status": "connected", "status_reason": None, "tools": entries, "tools_hash": tools_hash(entries)}
        except UpstreamFailure as exc:
            update = {"status": "unreachable", "status_reason": exc.reason}
        except Exception as exc:  # never let one server stop the sweep
            log.exception("probe of %s failed", slug)
            update = {"status": "unreachable", "status_reason": f"unexpected {type(exc).__name__}"}
        metrics.UPSTREAM_UP.labels(slug).set(1 if update["status"] == "connected" else 0)
        metrics.PROBE_SECONDS.labels(slug).observe(time.monotonic() - started)
        async with self.db.session() as s:
            server = await s.get(UpstreamServer, server_id)
            if server is None:
                return
            if update["status"] != server.status:
                log.info("upstream %s: %s -> %s %s", slug, server.status, update["status"], update.get("status_reason") or "")
            for k, v in update.items():
                setattr(server, k, v)
            server.checked_at = utcnow()
            await s.commit()

    async def mark_unreachable(self, server_id: int, reason: str) -> None:
        """A live call just failed to connect: show it now instead of at the next sweep."""
        async with self.db.session() as s:
            server = await s.get(UpstreamServer, server_id)
            if server and server.enabled:
                server.status, server.status_reason, server.checked_at = "unreachable", reason, utcnow()
                await s.commit()
