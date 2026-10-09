from __future__ import annotations

import mcp_types as types
import pytest

from gateway.chat import _clean_schema
from gateway.security import hash_password, sign_token, verify_password, verify_token
from gateway.ssrf import UnsafeURL, check_url
from gateway.upstreams import expose


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8080/mcp",
        "http://localhost/mcp",
        "http://[::1]/mcp",
        "http://10.0.0.5/mcp",
        "http://metadata.google.internal/computeMetadata/v1",
        "file:///etc/passwd",
        "http://user:pw@example.com/mcp",
        "http://kubernetes.default.svc.cluster.local/api",
    ],
)
async def test_ssrf_guard_refuses(url):
    with pytest.raises(UnsafeURL):
        await check_url(url, ["*.svc.cluster.local"])


async def test_ssrf_guard_allows_public_host():
    await check_url("https://example.com/mcp", [])


def test_tool_names_are_namespaced_sanitized_and_unique():
    tools = [types.Tool(name=n, input_schema={"type": "object"}) for n in ("get_order", "get.order", "a" * 80)]
    names = [e["name"] for e in expose("shipping", tools)]
    assert names[0] == "shipping__get_order"
    assert names[1] == "shipping__get_order_2"  # '.' sanitised, clash suffixed
    assert len(names[2]) == 64 and names[2].startswith("shipping__a")


def test_passwords_and_tokens():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h) and not verify_password("wrong", h)
    t = sign_token("c", {"uid": 3}, "k" * 32, 60)
    assert verify_token(t, "c", "k" * 32)["uid"] == 3
    assert verify_token(t, "s", "k" * 32) is None  # a chat token is not a session
    assert verify_token(t, "c", "other") is None
    assert verify_token(sign_token("c", {"uid": 3}, "k" * 32, -1), "c", "k" * 32) is None


def test_schema_cleaning_for_gemini():
    schema = {
        "type": "object",
        "$defs": {"S": {"enum": ["a", "b"], "type": "string"}},
        "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "X"}, "s": {"$ref": "#/$defs/S"}},
        "required": ["s"],
        "additionalProperties": False,
    }
    out = _clean_schema(schema)
    assert out == {"type": "object", "properties": {"x": {"type": "string"}, "s": {"type": "string", "enum": ["a", "b"]}}, "required": ["s"]}
