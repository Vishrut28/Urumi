"""Gateway process: dashboard REST API on /api, the aggregated MCP endpoint on /mcp."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway.api import router
from gateway.config import Settings, get_settings
from gateway.db import Database
from gateway.mcp_endpoint import Gateway, McpAuth
from gateway.security import CredentialBox
from gateway.seed import seed
from gateway.upstreams import HealthMonitor

log = logging.getLogger("gateway")


def create_app(settings: Settings | None = None, *, start_monitor: bool = True) -> ASGIApp:
    settings = settings or get_settings()
    settings.require_secrets()
    db = Database(settings.database_url)
    box = CredentialBox(settings.credentials_key)
    allowed = [p.strip() for p in settings.ssrf_allowed_internal_hosts.split(",")]
    monitor = HealthMonitor(db, box, settings.health_interval_seconds, settings.health_timeout_seconds, allowed, settings.ssrf_allow_all)
    gateway = Gateway(settings, db, box, monitor)
    mcp_app = gateway.server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0", streamable_http_path="/mcp")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await db.create_all()
        await seed(settings, db, box)
        async with gateway.server.session_manager.run():
            if start_monitor:
                monitor.start()
            try:
                yield
            finally:
                await monitor.stop()
                await db.dispose()

    api = FastAPI(title="MCP Gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    api.state.gateway = gateway
    api.include_router(router)
    if settings.provisioner_enabled:
        from gateway.stores import router as stores_router

        api.include_router(stores_router)

    @api.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @api.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            async with db.session() as s:
                await s.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse({"ok": False, "reason": type(exc).__name__}, status_code=503)
        return JSONResponse({"ok": True})

    @api.get("/metrics")
    async def metrics_endpoint() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    authed_mcp = McpAuth(mcp_app, gateway)

    async def dispatch(scope: Scope, receive: Receive, send: Send) -> None:
        # /mcp goes straight to the MCP transport (no redirect for a missing
        # trailing slash, which some clients do not follow); everything else,
        # lifespan included, goes to FastAPI.
        if scope["type"] == "http" and (scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
            await authed_mcp(scope, receive, send)
        else:
            await api(scope, receive, send)

    dispatch.state = api.state  # type: ignore[attr-defined]
    return dispatch


def app() -> ASGIApp:  # uvicorn --factory gateway.main:app
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    return create_app()
