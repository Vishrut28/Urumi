"""Passwords, API keys, signed tokens and credential encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

API_KEY_PREFIX = "mgw_"


# Passwords: scrypt from the standard library, per-password salt.
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, n, r, p, salt_b64, digest_b64 = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


# API keys: 256 bits of randomness, so a keyed fast hash is enough (no brute-force
# to slow down) and allows a direct indexed lookup. The pepper lives in a Secret,
# so a leaked database alone cannot be used to test candidate keys.
def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), key.encode(), hashlib.sha256).hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Signed, expiring tokens: dashboard sessions ("s") and chat-agent MCP tokens ("c").
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_token(kind: str, payload: dict[str, Any], secret: str, ttl_seconds: int) -> str:
    body = _b64(json.dumps({**payload, "k": kind, "exp": int(time.time()) + ttl_seconds}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"{kind}.{body}".encode(), hashlib.sha256).digest())
    return f"{kind}.{body}.{sig}"


def verify_token(token: str, kind: str, secret: str) -> dict[str, Any] | None:
    try:
        got_kind, body, sig = token.split(".")
    except ValueError:
        return None
    if got_kind != kind:
        return None
    expected = _b64(hmac.new(secret.encode(), f"{kind}.{body}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if payload.get("k") != kind or payload.get("exp", 0) < time.time():
        return None
    return payload


class CredentialBox:
    """Encrypts upstream credentials at rest. The key is a Fernet key from a Secret."""

    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def seal(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def open(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError("upstream credential cannot be decrypted (wrong CREDENTIALS_KEY?)") from exc
