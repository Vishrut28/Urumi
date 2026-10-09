"""woo-mcp against a fake WooCommerce that verifies OAuth 1.0a the way the PHP does."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from urllib.parse import parse_qsl, quote, urlsplit

os.environ["WOO_MCP_NO_APP"] = "1"

import httpx
import pytest
from mcp import Client

from woo_mcp import server
from woo_mcp.wc import WooClient

KEY, SECRET = "ck_test", "cs_test"
HOME = "https://shop.example.test"  # public home URL; requests go to the in-cluster host


def rfc3986(s: str) -> str:  # PHP rawurlencode
    return quote(s, safe="-_.~")


def php_check_signature(method: str, path: str, params: dict[str, str]) -> bool:
    """Line-for-line port of WC_REST_Authentication::check_oauth_signature (is_ssl() false)."""
    home = urlsplit(HOME)
    base_request_uri = rfc3986(f"http://{home.netloc}{path}")
    consumer_signature = params.pop("oauth_signature")
    keys = sorted(params)  # uksort strcmp
    normalized = {rfc3986(k): rfc3986(params[k]) for k in keys}
    query_string = "%26".join(rfc3986(f"{k}={v}") for k, v in normalized.items())
    string_to_sign = f"{method}&{base_request_uri}&{query_string}"
    sig = base64.b64encode(hmac.new(f"{SECRET}&".encode(), string_to_sign.encode(), hashlib.sha256).digest()).decode()
    return hmac.compare_digest(sig, consumer_signature)


ORDERS = [
    {
        "id": 42, "number": "42", "status": "processing", "total": "19.99", "currency": "USD",
        "date_created": "2026-10-09T10:00:00", "payment_method_title": "Cash on delivery",
        "billing": {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.test"},
        "line_items": [{"product_id": 7, "name": "Demo Mug", "quantity": 1, "total": "19.99"}],
    }
]
seen: list[httpx.Request] = []


def handler(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    path = request.url.path
    if path == "/wp-json/":
        return httpx.Response(200, json={"name": "Demo", "home": HOME, "timezone_string": "UTC", "gmt_offset": 0})
    params = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
    if not php_check_signature(request.method, path, params):
        return httpx.Response(401, json={"code": "woocommerce_rest_authentication_error", "message": "Invalid signature - provided signature does not match."})
    if path == "/wp-json/wc/v3/orders":
        return httpx.Response(200, json=ORDERS)
    if path == "/wp-json/wc/v3/products" and request.method == "POST":
        body = json.loads(request.content)
        return httpx.Response(201, json={"id": 99, "status": body["status"], "permalink": f"{HOME}/product/x", **body})
    return httpx.Response(404, json={"code": "rest_no_route", "message": "No route was found"})


@pytest.fixture(autouse=True)
def fake_store(monkeypatch):
    seen.clear()
    monkeypatch.setattr(server, "woo", WooClient("http://wordpress.store", KEY, SECRET, transport=httpx.MockTransport(handler)))


async def test_list_orders_today_signed_and_windowed():
    async with Client(server.mcp) as c:
        result = await c.call_tool("list_orders", {"period": "today"})
    assert not result.is_error, result.content
    data = result.structured_content
    assert data["count"] == 1 and data["orders"][0]["customer"] == "Ada Lovelace"
    assert data["window"]["after"].endswith("T00:00:00")
    # ':' in the date must survive double encoding in the signature base string.
    assert "after" in dict(parse_qsl(seen[-1].url.query.decode()))


async def test_create_product_posts_json_and_signs_query_only():
    async with Client(server.mcp) as c:
        result = await c.call_tool("create_product", {"name": "Agent Mug", "regular_price": "12.50"})
    assert not result.is_error, result.content
    assert result.structured_content["name"] == "Agent Mug"
    assert json.loads(seen[-1].content)["regular_price"] == "12.50"


async def test_bad_secret_is_a_clear_tool_error(monkeypatch):
    monkeypatch.setattr(server, "woo", WooClient("http://wordpress.store", KEY, "wrong", transport=httpx.MockTransport(handler)))
    async with Client(server.mcp) as c:
        result = await c.call_tool("get_order", {"order_id": 42})
    assert result.is_error
    assert "HTTP 401" in result.content[0].text


async def test_invalid_price_rejected_before_store():
    async with Client(server.mcp) as c:
        result = await c.call_tool("create_product", {"name": "x", "regular_price": "free"})
    assert result.is_error
    assert not any(r.method == "POST" for r in seen)


async def test_tools_exposed():
    async with Client(server.mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert {"list_products", "create_product", "list_orders", "get_order", "update_order_status"} <= names
