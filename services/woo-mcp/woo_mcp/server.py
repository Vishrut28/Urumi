"""WooCommerce MCP server: the store's products and orders as MCP tools over Streamable HTTP.

Only the gateway may call it. Two things enforce that: the Service has no
Ingress and a NetworkPolicy admits only gateway pods, and every request must
carry the bearer token held in this server's Secret, which the gateway keeps
server-side as the upstream credential.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Annotated, Any, Literal

import mcp_types as types
from mcp.server.mcpserver import MCPServer
from pydantic import Field
from starlette.types import ASGIApp, Receive, Scope, Send

from woo_mcp.wc import StoreError, WooClient

log = logging.getLogger("woo_mcp")

STORE_URL = os.environ.get("WC_STORE_URL", "http://wordpress")
woo = WooClient(
    STORE_URL,
    os.environ.get("WC_CONSUMER_KEY", ""),
    os.environ.get("WC_CONSUMER_SECRET", ""),
    timeout=float(os.environ.get("WC_TIMEOUT_SECONDS", "15")),
)

mcp = MCPServer(
    "woocommerce",
    version="1.0.0",
    instructions="Products and orders of one WooCommerce store. Prices are strings in the store currency.",
)

READ = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

OrderStatus = Literal["pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"]


def _product(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p["id"],
        "name": p["name"],
        "sku": p.get("sku") or None,
        "price": p.get("price") or p.get("regular_price") or None,
        "status": p["status"],
        "stock_status": p.get("stock_status"),
        "stock_quantity": p.get("stock_quantity"),
        "permalink": p.get("permalink"),
    }


def _order(o: dict[str, Any], full: bool = False) -> dict[str, Any]:
    billing = o.get("billing") or {}
    out: dict[str, Any] = {
        "id": o["id"],
        "number": o.get("number"),
        "status": o["status"],
        "total": o["total"],
        "currency": o["currency"],
        "date_created": o["date_created"],
        "customer": " ".join(x for x in (billing.get("first_name"), billing.get("last_name")) if x) or None,
        "payment_method": o.get("payment_method_title"),
        "items": [{"product_id": i["product_id"], "name": i["name"], "quantity": i["quantity"], "total": i["total"]} for i in o.get("line_items", [])],
    }
    if full:
        out.update(
            email=billing.get("email"),
            billing_address=", ".join(x for x in (billing.get("address_1"), billing.get("city"), billing.get("postcode"), billing.get("country")) if x) or None,
            customer_note=o.get("customer_note") or None,
            date_modified=o.get("date_modified"),
        )
    return out


@mcp.tool(annotations=READ)
async def get_store_info() -> dict[str, Any]:
    """Store name, home URL, timezone and currency. Useful before reasoning about 'today'."""
    site = await woo.site()
    tz = await woo.tz()
    currency = await woo.request("GET", "settings/general/woocommerce_currency")
    return {"name": site.get("name"), "url": site.get("home"), "timezone": str(tz), "currency": currency.get("value")}


@mcp.tool(annotations=READ)
async def list_products(
    search: Annotated[str | None, Field(description="Text to match in product name or description")] = None,
    status: Annotated[Literal["any", "publish", "draft", "pending", "private"], Field(description="Publication status")] = "any",
    per_page: Annotated[int, Field(ge=1, le=100)] = 20,
    page: Annotated[int, Field(ge=1)] = 1,
) -> list[dict[str, Any]]:
    """List products in the store, newest first."""
    rows = await woo.request("GET", "products", {"search": search, "status": status, "per_page": per_page, "page": page, "orderby": "date", "order": "desc"})
    return [_product(p) for p in rows]


@mcp.tool(annotations=WRITE)
async def create_product(
    name: Annotated[str, Field(min_length=1, max_length=200)],
    regular_price: Annotated[str, Field(pattern=r"^\d+(\.\d{1,2})?$", description="Price as a decimal string, e.g. '19.99'")],
    description: str | None = None,
    sku: str | None = None,
    stock_quantity: Annotated[int | None, Field(ge=0, description="Set to manage stock; leave empty for unlimited")] = None,
    status: Literal["publish", "draft"] = "publish",
) -> dict[str, Any]:
    """Create a simple product. Published products appear on the storefront immediately."""
    body: dict[str, Any] = {"name": name, "type": "simple", "regular_price": regular_price, "status": status}
    if description:
        body["description"] = description
    if sku:
        body["sku"] = sku
    if stock_quantity is not None:
        body.update(manage_stock=True, stock_quantity=stock_quantity)
    return _product(await woo.request("POST", "products", json=body))


@mcp.tool(annotations=READ)
async def list_orders(
    period: Annotated[
        Literal["today", "yesterday", "last_7_days", "last_30_days", "all"],
        Field(description="Creation-date window in the store's timezone"),
    ] = "all",
    status: Annotated[OrderStatus | Literal["any"], Field(description="Order status filter")] = "any",
    per_page: Annotated[int, Field(ge=1, le=100)] = 20,
    page: Annotated[int, Field(ge=1)] = 1,
) -> dict[str, Any]:
    """List orders, newest first, optionally limited to a period such as today."""
    params: dict[str, Any] = {"status": status, "per_page": per_page, "page": page, "orderby": "date", "order": "desc"}
    window = None
    if period != "all":
        after, before = await woo.day_bounds(period)
        params.update(after=after, before=before)
        window = {"after": after, "before": before, "timezone": str(await woo.tz())}
    rows = await woo.request("GET", "orders", params)
    return {"period": period, "window": window, "count": len(rows), "orders": [_order(o) for o in rows]}


@mcp.tool(annotations=READ)
async def get_order(order_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
    """Full details of one order, including customer, address and line items."""
    return _order(await woo.request("GET", f"orders/{order_id}"), full=True)


@mcp.tool(annotations=WRITE)
async def update_order_status(
    order_id: Annotated[int, Field(ge=1)],
    status: OrderStatus,
    note: Annotated[str | None, Field(max_length=500, description="Optional private order note explaining the change")] = None,
) -> dict[str, Any]:
    """Change an order's status, e.g. to 'completed' once it has shipped."""
    order = await woo.request("PUT", f"orders/{order_id}", json={"status": status})
    if note:
        await woo.request("POST", f"orders/{order_id}/notes", json={"note": note, "customer_note": False})
    return _order(order)


class BearerAuth:
    """Reject MCP requests without the shared bearer token; serve health checks unauthenticated."""

    def __init__(self, app: ASGIApp, token: str):
        if not token:
            raise RuntimeError("MCP_AUTH_TOKEN is not set")
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if scope["path"] in ("/healthz", "/readyz"):
                await _send_json(send, 200, {"ok": True})
                return
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await _send_json(send, 401, {"error": "unauthorized"})
                return
        await self.app(scope, receive, send)


async def _send_json(send: Send, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": payload})


def build_app() -> ASGIApp:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    return BearerAuth(app, os.environ.get("MCP_AUTH_TOKEN", ""))


app = build_app() if os.environ.get("WOO_MCP_NO_APP") != "1" else None
