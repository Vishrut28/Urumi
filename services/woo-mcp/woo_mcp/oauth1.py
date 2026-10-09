"""One-legged OAuth 1.0a signing, as WooCommerce verifies it over plain HTTP.

WooCommerce accepts HTTP Basic auth only when `is_ssl()` is true. Inside the
cluster the MCP server talks to WordPress over plain HTTP, so it signs every
request instead (class-wc-rest-authentication.php, check_oauth_signature):

- the parameters are the query string plus the oauth_* fields, never a JSON body;
- each key and value is RFC 3986 encoded, pairs sorted by key, joined with `&`,
  and that whole string is encoded again;
- the base URL is the site's *home URL* plus the request path, with the scheme
  forced to http when the request did not arrive over TLS - not the in-cluster
  host name the request was actually sent to;
- the HMAC key is the consumer secret followed by `&`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from urllib.parse import quote


def _enc(value: str) -> str:
    return quote(value, safe="~-._")


def sign(
    method: str,
    base_url: str,
    params: Mapping[str, str],
    consumer_key: str,
    consumer_secret: str,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return `params` plus the oauth_* fields, including oauth_signature."""
    signed = {str(k): str(v) for k, v in params.items()}
    signed.update(
        oauth_consumer_key=consumer_key,
        oauth_nonce=nonce or secrets.token_hex(16),
        oauth_signature_method="HMAC-SHA256",
        oauth_timestamp=str(timestamp if timestamp is not None else int(time.time())),
    )
    pairs = sorted((_enc(k), _enc(v)) for k, v in signed.items())
    param_string = "&".join(f"{k}={v}" for k, v in pairs)
    base_string = "&".join([method.upper(), _enc(base_url), _enc(param_string)])
    digest = hmac.new(f"{consumer_secret}&".encode(), base_string.encode(), hashlib.sha256).digest()
    signed["oauth_signature"] = base64.b64encode(digest).decode()
    return signed
