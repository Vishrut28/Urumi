"""Utilities MCP server: currency conversion, time zones and arithmetic.

Deliberately has no credential, to show the gateway's "no auth" upstream type;
its NetworkPolicy still admits only the gateway. Exchange rates are a fixed
table so the demo never depends on an outside API.
"""

from __future__ import annotations

import ast
import json
import operator
import os
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import mcp_types as types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.types import ASGIApp, Receive, Scope, Send

mcp = MCPServer("utilities", version="1.0.0", instructions="Currency conversion, current time and arithmetic.")
READ = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Units of each currency per 1 USD. Fixed demo rates, not live market data.
RATES_PER_USD = {
    "USD": Decimal("1"), "EUR": Decimal("0.92"), "GBP": Decimal("0.79"), "INR": Decimal("83.50"),
    "JPY": Decimal("149.00"), "CAD": Decimal("1.37"), "AUD": Decimal("1.52"), "CHF": Decimal("0.88"),
}
RATES_AS_OF = "2026-10-01"


@mcp.tool(annotations=READ)
def convert_currency(
    amount: Annotated[str, Field(pattern=r"^-?\d+(\.\d+)?$", description="Decimal amount, e.g. '19.99'")],
    from_currency: Annotated[str, Field(min_length=3, max_length=3)],
    to_currency: Annotated[str, Field(min_length=3, max_length=3)],
) -> dict[str, Any]:
    """Convert an amount between currencies using a fixed daily rate table (USD, EUR, GBP, INR, JPY, CAD, AUD, CHF)."""
    src, dst = from_currency.upper(), to_currency.upper()
    for code in (src, dst):
        if code not in RATES_PER_USD:
            raise ToolError(f"unsupported currency {code}; supported: {', '.join(RATES_PER_USD)}")
    converted = (Decimal(amount) / RATES_PER_USD[src] * RATES_PER_USD[dst]).quantize(Decimal("0.01"), ROUND_HALF_UP)
    rate = (RATES_PER_USD[dst] / RATES_PER_USD[src]).quantize(Decimal("0.000001"))
    return {"amount": amount, "from": src, "to": dst, "converted": str(converted), "rate": str(rate), "rates_as_of": RATES_AS_OF}


@mcp.tool(annotations=READ)
def current_time(timezone: Annotated[str, Field(description="IANA zone, e.g. 'Europe/Berlin' or 'UTC'")] = "UTC") -> dict[str, str]:
    """Current date and time in a time zone."""
    try:
        now = datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolError(f"unknown time zone {timezone!r}") from None
    return {"timezone": timezone, "iso": now.isoformat(timespec="seconds"), "date": now.date().isoformat(), "weekday": now.strftime("%A")}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Mod: operator.mod}


def _eval(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return Decimal(str(node.value))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        if isinstance(node.op, ast.Pow) and abs(_eval(node.right)) > 100:
            raise ToolError("exponent too large")
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ToolError("only numbers, + - * / % ** and parentheses are allowed")


@mcp.tool(annotations=READ)
def calculate(expression: Annotated[str, Field(max_length=200, description="Arithmetic such as '(19.99 + 5) * 2'")]) -> dict[str, str]:
    """Evaluate an arithmetic expression exactly (decimal arithmetic, no variables or functions)."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ToolError("not a valid arithmetic expression") from None
    try:
        result = _eval(tree)
    except ZeroDivisionError:
        raise ToolError("division by zero") from None
    return {"expression": expression, "result": str(result.normalize() if result == result.to_integral() else result)}


class Health:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in ("/healthz", "/readyz"):
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps({"ok": True}).encode()})
            return
        await self.app(scope, receive, send)


app = Health(mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")) if os.environ.get("NO_APP") != "1" else None
