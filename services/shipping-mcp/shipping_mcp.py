"""Shipping MCP server: rate quotes and shipments for store orders.

A stand-in for a carrier integration. It authenticates with a static header
credential (X-API-Key), the gateway's second credential type. It also exposes a
tool named `get_order` - the same name as the WooCommerce server's - so the
gateway's collision handling is exercised by real servers, not a unit test.

Shipments live in memory: a restarted pod forgets them, which is fine for a
demo carrier and keeps the server free of storage.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

import mcp_types as types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.types import ASGIApp, Receive, Scope, Send

mcp = MCPServer("shipping", version="1.0.0", instructions="Shipping quotes and shipments. Shipments are keyed by the store's order id.")
READ = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

Carrier = Literal["dhl", "ups", "fedex", "local-post"]
BASE = {"dhl": Decimal("9.50"), "ups": Decimal("8.75"), "fedex": Decimal("10.20"), "local-post": Decimal("4.90")}
PER_KG = {"dhl": Decimal("2.10"), "ups": Decimal("2.40"), "fedex": Decimal("1.95"), "local-post": Decimal("1.20")}
DAYS = {"dhl": 3, "ups": 4, "fedex": 2, "local-post": 6}

shipments: dict[int, dict[str, Any]] = {}  # store order id -> shipment


def _quote(carrier: str, weight_kg: float, international: bool) -> dict[str, Any]:
    price = BASE[carrier] + PER_KG[carrier] * Decimal(str(weight_kg))
    if international:
        price *= Decimal("1.8")
    return {"carrier": carrier, "price_usd": str(price.quantize(Decimal("0.01"))), "estimated_days": DAYS[carrier] + (3 if international else 0)}


@mcp.tool(annotations=READ)
def get_shipping_quote(
    weight_kg: Annotated[float, Field(gt=0, le=70)],
    destination_country: Annotated[str, Field(min_length=2, max_length=2, description="ISO country code, e.g. 'US'")],
    origin_country: Annotated[str, Field(min_length=2, max_length=2)] = "US",
) -> list[dict[str, Any]]:
    """Price and delivery estimate from every carrier, cheapest first."""
    international = destination_country.upper() != origin_country.upper()
    return sorted((_quote(c, weight_kg, international) for c in BASE), key=lambda q: Decimal(q["price_usd"]))


@mcp.tool(annotations=WRITE)
def create_shipment(
    order_id: Annotated[int, Field(ge=1, description="The store's order id")],
    carrier: Carrier = "local-post",
    weight_kg: Annotated[float, Field(gt=0, le=70)] = 1.0,
) -> dict[str, Any]:
    """Book a shipment for a store order and get its tracking number. One shipment per order."""
    if order_id in shipments:
        raise ToolError(f"order {order_id} already has shipment {shipments[order_id]['tracking_number']}")
    now = datetime.now(UTC)
    shipments[order_id] = {
        "order_id": order_id,
        "carrier": carrier,
        "tracking_number": f"{carrier[:3].upper()}{secrets.randbelow(10**10):010d}",
        "status": "label_created",
        "weight_kg": weight_kg,
        "created_at": now.isoformat(timespec="seconds"),
        "estimated_delivery": (now + timedelta(days=DAYS[carrier])).date().isoformat(),
    }
    return shipments[order_id]


@mcp.tool(annotations=READ)
def get_order(order_id: Annotated[int, Field(ge=1, description="The store's order id")]) -> dict[str, Any]:
    """The shipping record for a store order: carrier, tracking number and delivery status."""
    if order_id not in shipments:
        raise ToolError(f"no shipment for order {order_id}")
    return shipments[order_id]


@mcp.tool(annotations=READ)
def list_shipments(status: Literal["any", "label_created", "in_transit", "delivered"] = "any") -> list[dict[str, Any]]:
    """All shipments, newest first."""
    rows = [s for s in shipments.values() if status == "any" or s["status"] == status]
    return sorted(rows, key=lambda s: s["created_at"], reverse=True)


class ApiKeyAuth:
    def __init__(self, app: ASGIApp, key: str):
        if not key:
            raise RuntimeError("SHIPPING_API_KEY is not set")
        self.app = app
        self.key = key.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if scope["path"] in ("/healthz", "/readyz"):
                return await _json(send, 200, {"ok": True})
            if not hmac.compare_digest(dict(scope["headers"]).get(b"x-api-key", b""), self.key):
                return await _json(send, 401, {"error": "unauthorized"})
        await self.app(scope, receive, send)


async def _json(send: Send, status: int, body: dict[str, Any]) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


app = (
    ApiKeyAuth(mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0"), os.environ.get("SHIPPING_API_KEY", ""))
    if os.environ.get("NO_APP") != "1"
    else None
)
