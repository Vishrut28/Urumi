"""End-to-end: real upstream servers and the gateway over HTTP, real MCP clients, a fake LLM.

Covers the definition-of-done control flows without Kubernetes: namespacing and
collisions, member vs admin visibility, key revocation, disable, a dead
upstream, audit attribution, and the chat agent's tool loop acting as the user.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import httpx
import httpx2
import pytest
import uvicorn
from cryptography.fernet import Fernet
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parents[2]
os.environ["NO_APP"] = "1"
sys.path[:0] = [str(ROOT / "utils-mcp"), str(ROOT / "shipping-mcp")]

import shipping_mcp  # noqa: E402
import utils_mcp  # noqa: E402

from gateway.config import Settings  # noqa: E402
from gateway.main import create_app  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Served:
    def __init__(self, app, port: int):
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
        self.task: asyncio.Task | None = None

    async def start(self):
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.05)

    async def stop(self):
        self.server.should_exit = True
        await self.task


# A fake OpenAI-compatible LLM: first turn calls utilities__convert_currency, second turn answers.
llm_requests: list[dict] = []


async def fake_completions(request: Request) -> JSONResponse:
    body = await request.json()
    llm_requests.append(body)
    if body["model"] == "retired-model":  # Gemini's shape for a model that was withdrawn
        return JSONResponse([{"error": {"code": 404, "message": "This model models/retired-model is no longer available to new users.", "status": "NOT_FOUND"}}], status_code=404)
    if body["messages"][-1]["role"] == "tool":
        return JSONResponse({"choices": [{"message": {"role": "assistant", "content": f"Result: {body['messages'][-1]['content']}"}}]})
    names = [t["function"]["name"] for t in body.get("tools", [])]
    assert "utilities__convert_currency" in names
    call = {"id": "call_1", "type": "function", "function": {"name": "utilities__convert_currency", "arguments": json.dumps({"amount": "100", "from_currency": "USD", "to_currency": "EUR"})}}
    return JSONResponse({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]})


@pytest.fixture
async def stack(tmp_path):
    utils_port, ship_port, llm_port, gw_port = free_port(), free_port(), free_port(), free_port()
    utils = Served(utils_mcp.Health(utils_mcp.mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")), utils_port)
    ship = Served(shipping_mcp.ApiKeyAuth(shipping_mcp.mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0"), "ship-secret"), ship_port)
    llm = Served(Starlette(routes=[Route("/v1/chat/completions", fake_completions, methods=["POST"])]), llm_port)
    os.environ["UPSTREAM_SHIPPING_KEY"] = "ship-secret"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'gw.db'}",
        session_secret="s" * 32, api_key_pepper="p" * 32, credentials_key=Fernet.generate_key().decode(),
        admin_email="alice@example.com", admin_password="alice-password-1",
        llm_base_url=f"http://127.0.0.1:{llm_port}/v1", llm_api_key="fake", llm_model="fake-model",
        health_interval_seconds=1, health_timeout_seconds=2, ssrf_allow_all=True, provisioner_enabled=True,
        self_mcp_url=f"http://127.0.0.1:{gw_port}/mcp", public_url=f"http://127.0.0.1:{gw_port}",
        seed_servers=json.dumps([
            {"slug": "utilities", "name": "Utilities", "url": f"http://127.0.0.1:{utils_port}/mcp"},
            {"slug": "shipping", "name": "Shipping", "url": f"http://127.0.0.1:{ship_port}/mcp", "auth_type": "header", "auth_header": "X-API-Key",
             "credential_env": "UPSTREAM_SHIPPING_KEY", "admin_only_tools": ["create_shipment"]},
        ]),
    )
    gw = Served(create_app(settings), gw_port)
    for s in (utils, ship, llm, gw):
        await s.start()
    base = f"http://127.0.0.1:{gw_port}"
    admin = httpx.AsyncClient(base_url=base, headers={"X-Requested-With": "dashboard"})
    r = await admin.post("/api/auth/login", json={"email": "alice@example.com", "password": "alice-password-1"})
    assert r.status_code == 200, r.text
    # wait for the first health sweep to cache tools
    for _ in range(50):
        servers = (await admin.get("/api/servers")).json()
        if all(s["status"] == "connected" for s in servers):
            break
        await asyncio.sleep(0.2)
    yield {"base": base, "admin": admin, "utils": utils, "ship": ship, "settings": settings}
    await admin.aclose()
    for s in (gw, llm, ship, utils):
        if s.task and not s.task.done():
            await s.stop()


async def invite_member(admin: httpx.AsyncClient, base: str, email: str) -> tuple[httpx.AsyncClient, str]:
    link = (await admin.post("/api/invites", json={"email": email, "role": "member"})).json()["link"]
    token = link.split("token=")[1]
    member = httpx.AsyncClient(base_url=base, headers={"X-Requested-With": "dashboard"})
    r = await member.post("/api/invites/accept", json={"token": token, "name": "Bob", "password": "bob-password-12"})
    assert r.status_code == 200, r.text
    key = (await member.post("/api/keys", json={"name": "laptop"})).json()["key"]
    return member, key


def mcp_client(base: str, key: str) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"})
    return Client(streamable_http_client(f"{base}/mcp", http_client=http))


async def test_definition_of_done_control_flow(stack):
    base, admin = stack["base"], stack["admin"]
    member, bob_key = await invite_member(admin, base, "bob@example.com")
    alice_key = (await admin.post("/api/keys", json={"name": "alice-cli"})).json()["key"]

    # Bob sees every enabled server's tools, namespaced; admin-only tool hidden from him.
    async with mcp_client(base, bob_key) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"utilities__convert_currency", "shipping__get_order", "shipping__get_shipping_quote"} <= names
        assert "shipping__create_shipment" not in names
        r = await c.call_tool("utilities__convert_currency", {"amount": "10", "from_currency": "USD", "to_currency": "EUR"})
        assert not r.is_error and r.structured_content["converted"] == "9.20"
        denied = await c.call_tool("shipping__create_shipment", {"order_id": 1})
        assert denied.is_error and "restricted to admins" in denied.content[0].text

    # Alice (admin) can use it; the header credential reached the upstream, not the client.
    async with mcp_client(base, alice_key) as c:
        r = await c.call_tool("shipping__create_shipment", {"order_id": 7})
        assert not r.is_error, r.content
        r = await c.call_tool("shipping__get_order", {"order_id": 7})
        assert r.structured_content["order_id"] == 7

    # Disable shipping: its tools disappear for everyone.
    servers = {s["slug"]: s for s in (await admin.get("/api/servers")).json()}
    assert "url" not in (await member.get("/api/servers")).json()[0]  # members never see upstream URLs
    assert all("credential" not in json.dumps(s).lower().replace("has_credential", "") for s in servers.values())
    await admin.patch(f"/api/servers/{servers['shipping']['id']}", json={"enabled": False})
    async with mcp_client(base, bob_key) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert not any(n.startswith("shipping__") for n in names) and "utilities__calculate" in names
        gone = await c.call_tool("shipping__get_order", {"order_id": 7})
        assert gone.is_error
    await admin.patch(f"/api/servers/{servers['shipping']['id']}", json={"enabled": True})

    # Kill the utilities upstream: shipping keeps working, utilities is reported unreachable.
    await stack["utils"].stop()
    async with mcp_client(base, alice_key) as c:
        r = await c.call_tool("utilities__calculate", {"expression": "1+1"})
        assert r.is_error and "Utilities" in r.content[0].text
        r = await c.call_tool("shipping__get_shipping_quote", {"weight_kg": 2, "destination_country": "DE"})
        assert not r.is_error
    status = {s["slug"]: s for s in (await admin.get("/api/servers")).json()}["utilities"]
    assert status["status"] == "unreachable" and status["status_reason"]

    # Revoke Bob's key: next call rejected, and the rejection is audited under Bob.
    keys = (await member.get("/api/keys")).json()
    await member.post(f"/api/keys/{keys[0]['id']}/revoke")
    with pytest.raises(BaseException):
        async with mcp_client(base, bob_key) as c:
            await c.call_tool("shipping__get_shipping_quote", {"weight_kg": 1, "destination_country": "US"})
    raw = await httpx.AsyncClient().post(f"{base}/mcp", headers={"Authorization": f"Bearer {bob_key}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "utilities__calculate", "arguments": {}}})
    assert raw.status_code == 401 and "revoked" in raw.text

    log = (await admin.get("/api/audit")).json()
    by_user = {(r["user_email"], r["tool"], r["outcome"]) for r in log}
    assert ("bob@example.com", "convert_currency", "ok") in by_user
    assert ("bob@example.com", "create_shipment", "denied") in by_user
    assert ("alice@example.com", "create_shipment", "ok") in by_user
    assert ("alice@example.com", "calculate", "unreachable") in by_user
    assert ("bob@example.com", "calculate", "rejected") in by_user
    assert all(r["duration_ms"] >= 0 and r["ts"] for r in log)
    # Members only see their own audit rows.
    assert {r["user_email"] for r in (await member.get("/api/audit")).json()} <= {"bob@example.com", None}


async def test_chat_agent_calls_tools_as_the_logged_in_user(stack):
    base, admin = stack["base"], stack["admin"]
    member, _ = await invite_member(admin, base, "carol@example.com")
    r = await member.post("/api/chat", json={"messages": [{"role": "user", "content": "What is 100 USD in EUR?"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["steps"][0]["tool"] == "utilities__convert_currency" and body["steps"][0]["ok"]
    assert "92.00" in body["reply"]
    # the LLM never saw admin-only tools for this member, and the call is audited as carol via chat
    assert "shipping__create_shipment" not in [t["function"]["name"] for t in llm_requests[-2]["tools"]]
    row = (await admin.get("/api/audit")).json()[0]
    assert (row["user_email"], row["source"], row["tool"], row["outcome"]) == ("carol@example.com", "chat", "convert_currency", "ok")


async def test_unauthenticated_and_bad_keys_rejected(stack):
    async with httpx.AsyncClient() as c:
        for headers in ({}, {"Authorization": "Bearer mgw_nope"}, {"Authorization": "Bearer c.forged.sig"}):
            r = await c.post(f"{stack['base']}/mcp", headers=headers, json={})
            assert r.status_code == 401


async def test_csrf_header_required_for_dashboard_writes(stack):
    cookies = stack["admin"].cookies
    async with httpx.AsyncClient(base_url=stack["base"], cookies=cookies) as c:
        r = await c.post("/api/keys", json={"name": "x"})
        assert r.status_code == 403


async def test_llm_error_reaches_the_user_readably(stack):
    stack["settings"].llm_model = "retired-model"
    r = await stack["admin"].post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502
    assert "HTTP 404" in r.json()["detail"] and "no longer available" in r.json()["detail"]


async def test_store_api_is_idempotent_limited_and_scoped(stack):
    base, admin = stack["base"], stack["admin"]
    member, _ = await invite_member(admin, base, "dora@example.com")
    first = (await member.post("/api/stores", json={"name": "Berlin", "slug": "berlin"})).json()
    again = (await member.post("/api/stores", json={"name": "Berlin", "slug": "berlin"})).json()
    assert first["id"] == again["id"] and first["status"] == "provisioning"  # retrying the request is safe
    assert first["namespace"] == "store-berlin" and first["url"].startswith("http://berlin.")
    assert (await member.post("/api/stores", json={"name": "Paris", "slug": "paris"})).status_code == 200
    r = await member.post("/api/stores", json={"name": "Rome", "slug": "rome"})
    assert r.status_code == 429 and "limit" in r.json()["detail"]
    assert (await admin.post("/api/stores", json={"name": "Taken", "slug": "berlin"})).status_code == 409
    assert (await member.post("/api/stores", json={"name": "x", "slug": "main"})).status_code == 422
    other, _ = await invite_member(admin, base, "erin@example.com")
    assert (await other.get("/api/stores")).json() == []
    assert (await other.delete(f"/api/stores/{first['id']}")).status_code == 404
    assert (await member.delete(f"/api/stores/{first['id']}")).status_code == 200
    assert {s["slug"]: s["status"] for s in (await admin.get("/api/stores")).json()}["berlin"] == "deleting"
