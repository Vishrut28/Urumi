"""Definition-of-done smoke test against a running deployment, through its public Ingress.

  ADMIN_EMAIL=... ADMIN_PASSWORD=... python scripts/smoke_test.py
  (or scripts/smoke-test.sh, which reads them from the cluster Secret)

Needs `pip install mcp httpx`. Creates a member, keys, a product and orders in
the store; it does not clean them up (they are the evidence).
Optional: CHAT=1 also asks the chat agent (needs the LLM key configured).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

APP = os.environ.get("APP_URL", "http://app.localtest.me")
SHOP = os.environ.get("SHOP_URL", "http://shop.localtest.me")
RUN = str(int(time.time()))[-6:]
failures: list[str] = []


def check(ok: bool, label: str, detail: object = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def mcp(key: str) -> Client:
    return Client(streamable_http_client(f"{APP}/mcp", http_client=httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60)))


async def storefront_order(product_id: int) -> int:
    """Add to cart and check out with Cash on Delivery via the WooCommerce Store API (what the checkout page calls)."""
    async with httpx.AsyncClient(base_url=SHOP, timeout=60) as shop:
        r = await shop.get("/wp-json/wc/store/v1/cart")
        headers = {"Nonce": r.headers["Nonce"], "Cart-Token": r.headers["Cart-Token"]}
        r = await shop.post("/wp-json/wc/store/v1/cart/add-item", json={"id": product_id, "quantity": 1}, headers=headers)
        r.raise_for_status()
        address = {"first_name": "Smoke", "last_name": f"Test{RUN}", "address_1": "1 Test Street", "city": "San Francisco", "state": "CA",
                   "postcode": "94103", "country": "US", "email": f"smoke{RUN}@example.com", "phone": "5550100"}
        r = await shop.post("/wp-json/wc/store/v1/checkout", json={"billing_address": address, "shipping_address": address, "payment_method": "cod"}, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"checkout failed: HTTP {r.status_code} {r.text[:300]}")
        return int(r.json()["order_id"])


async def main() -> int:
    admin = httpx.AsyncClient(base_url=APP, headers={"X-Requested-With": "dashboard"}, timeout=120)
    r = await admin.post("/api/auth/login", json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]})
    r.raise_for_status()
    print("dashboard: admin signed in (user A)")

    servers = {s["slug"]: s for s in (await admin.get("/api/servers")).json()}
    check(all(servers[s]["status"] == "connected" for s in ("woocommerce", "shipping", "utilities")), "three upstreams connected", {k: v["status"] for k, v in servers.items()})

    # User B: invited member with their own key.
    email_b = f"bob{RUN}@example.com"
    link = (await admin.post("/api/invites", json={"email": email_b, "role": "member"})).json()["link"]
    bob = httpx.AsyncClient(base_url=APP, headers={"X-Requested-With": "dashboard"}, timeout=120)
    r = await bob.post("/api/invites/accept", json={"token": link.split("token=")[1], "name": "Bob", "password": f"bob-password-{RUN}"})
    check(r.status_code == 200, "user B accepted invite")
    bob_key = (await bob.post("/api/keys", json={"name": "smoke-test"})).json()["key"]
    alice_key = (await admin.post("/api/keys", json={"name": f"smoke-admin-{RUN}"})).json()["key"]

    print("store + MCP as user A (admin key):")
    async with mcp(alice_key) as c:
        r = await c.call_tool("woocommerce__create_product", {"name": f"Smoke Mug {RUN}", "regular_price": "14.50"})
        check(not r.is_error, "woocommerce__create_product", r.content[0].text if r.is_error else "")
        product = r.structured_content
    page = (await httpx.AsyncClient(timeout=60, follow_redirects=True).get(product["permalink"])).text
    check(f"Smoke Mug {RUN}" in page, "new product visible on the storefront", product["permalink"])

    order_id = await storefront_order(product["id"])
    print(f"storefront: checked out order #{order_id} with Cash on delivery")

    print("external MCP client as user B (member key):")
    async with mcp(bob_key) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        by_server = {s: sum(1 for t in tools if t.startswith(s + "__")) for s in ("woocommerce", "shipping", "utilities")}
        check(all(by_server.values()), "tools from every enabled server", by_server)
        check("woocommerce__get_order" in tools and "shipping__get_order" in tools, "colliding get_order tools both exposed, namespaced")
        check("woocommerce__create_product" not in tools, "admin-only tool hidden from member")
        r = await c.call_tool("woocommerce__list_orders", {"period": "today"})
        ids = [o["id"] for o in (r.structured_content or {}).get("orders", [])]
        check(not r.is_error and order_id in ids, "orders today includes the storefront order", r.content[0].text[:200] if r.is_error else ids)
        total = next(o["total"] for o in r.structured_content["orders"] if o["id"] == order_id)
        r = await c.call_tool("utilities__convert_currency", {"amount": total, "from_currency": "USD", "to_currency": "EUR"})
        check(not r.is_error, f"cross-server: order total {total} USD -> {r.structured_content and r.structured_content.get('converted')} EUR")
        r = await c.call_tool("woocommerce__update_order_status", {"order_id": order_id, "status": "completed"})
        check(r.is_error and "admins" in r.content[0].text, "member calling admin-only tool is rejected")

    if os.environ.get("CHAT") == "1":
        print("chat agent as user A:")
        r = await admin.post("/api/chat", json={"messages": [{"role": "user", "content": "What orders came in today? List their numbers."}]})
        body = r.json()
        check(r.status_code == 200 and any(s["tool"] == "woocommerce__list_orders" for s in body.get("steps", [])), "chat called woocommerce__list_orders", body)
        check(str(order_id) in body.get("reply", ""), "chat reply mentions the order", body.get("reply", "")[:200])

    print("team and control:")
    await admin.patch(f"/api/servers/{servers['shipping']['id']}", json={"enabled": False})
    async with mcp(bob_key) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
    check(not any(t.startswith("shipping__") for t in tools), "disabled server's tools disappear")
    await admin.patch(f"/api/servers/{servers['shipping']['id']}", json={"enabled": True})

    key_id = (await bob.get("/api/keys")).json()[0]["id"]
    await bob.post(f"/api/keys/{key_id}/revoke")
    r = await httpx.AsyncClient().post(f"{APP}/mcp", headers={"Authorization": f"Bearer {bob_key}", "Accept": "application/json, text/event-stream"},
                                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "woocommerce__list_orders", "arguments": {}}})
    check(r.status_code == 401, "revoked key rejected (401)", r.status_code)

    rows = (await admin.get("/api/audit?limit=200")).json()
    mine = {(x["user_email"], x["tool"], x["outcome"]) for x in rows}
    check((email_b, "list_orders", "ok") in mine, "audit: B's list_orders attributed to B")
    check((email_b, "update_order_status", "denied") in mine, "audit: B's denied call recorded")
    check((email_b, "list_orders", "rejected") in mine, "audit: B's call with revoked key recorded")
    check((os.environ["ADMIN_EMAIL"], "create_product", "ok") in mine, "audit: A's create_product attributed to A")

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
