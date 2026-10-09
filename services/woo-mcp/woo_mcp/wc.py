"""A small async client for the WooCommerce REST API (wc/v3)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from woo_mcp import oauth1


class StoreError(ToolError):
    """A store call failed; the message is safe to show to the caller."""


class WooClient:
    def __init__(
        self,
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._store_url = store_url.rstrip("/")
        self._key = consumer_key
        self._secret = consumer_secret
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._site: dict[str, Any] | None = None
        self._site_fetched = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def site(self) -> dict[str, Any]:
        """The public /wp-json/ index: home URL and timezone. Cached for five minutes."""
        if self._site is None or time.monotonic() - self._site_fetched > 300:
            try:
                resp = await self._http.get(f"{self._store_url}/wp-json/")
            except httpx.HTTPError as exc:
                raise StoreError(f"store unreachable: {type(exc).__name__}") from exc
            if resp.status_code != 200:
                raise StoreError(f"store index returned HTTP {resp.status_code}")
            self._site = resp.json()
            self._site_fetched = time.monotonic()
        return self._site

    async def tz(self) -> timezone | ZoneInfo:
        site = await self.site()
        if site.get("timezone_string"):
            return ZoneInfo(site["timezone_string"])
        return timezone(timedelta(hours=float(site.get("gmt_offset") or 0)))

    async def _signing_base(self, path: str) -> str:
        # WooCommerce signs against home_url(path) with the scheme forced to
        # http, because the in-cluster request does not arrive over TLS.
        home = urlsplit((await self.site())["home"])
        return urlunsplit(("http", home.netloc, home.path.rstrip("/") + path, "", ""))

    async def request(self, method: str, endpoint: str, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        path = f"/wp-json/wc/v3/{endpoint.lstrip('/')}"
        query = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in (params or {}).items() if v is not None}
        signed = oauth1.sign(method, await self._signing_base(path), query, self._key, self._secret)
        try:
            resp = await self._http.request(method, f"{self._store_url}{path}", params=signed, json=json)
        except httpx.TimeoutException as exc:
            raise StoreError("store did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise StoreError(f"store unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            try:
                body = resp.json()
                detail = body.get("message") or body.get("code") or resp.text[:200]
            except ValueError:
                detail = resp.text[:200]
            raise StoreError(f"store returned HTTP {resp.status_code}: {detail}")
        return resp.json()

    async def day_bounds(self, period: str) -> tuple[str, str | None]:
        """Local-time ISO bounds for a named period, in the store's timezone."""
        now = datetime.now(await self.tz())
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "today":
            start, end = start_today, None
        elif period == "yesterday":
            start, end = start_today - timedelta(days=1), start_today
        elif period == "last_7_days":
            start, end = start_today - timedelta(days=6), None
        elif period == "last_30_days":
            start, end = start_today - timedelta(days=29), None
        else:
            raise StoreError(f"unknown period {period!r}")
        fmt = "%Y-%m-%dT%H:%M:%S"
        return start.strftime(fmt), end.strftime(fmt) if end else None
