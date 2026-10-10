"""OAuth 2.1 authorization server for the gateway's MCP endpoint (MCP authorization spec).

Lets an MCP client connect with a browser login instead of a pasted API key:

  client --(no token)--> /mcp  =>  401 + WWW-Authenticate: resource_metadata=...
  client -> /.well-known/oauth-protected-resource[/mcp]   (RFC 9728)
  client -> /.well-known/oauth-authorization-server        (RFC 8414; OIDC discovery alias)
  client registers: Client ID Metadata Document (https URL as client_id) or POST /oauth/register (RFC 7591)
  browser -> /oauth/authorize (PKCE S256, resource) -> dashboard login -> consent page -> redirect with code + iss
  client -> POST /oauth/token (code + verifier + resource) -> access token (1 h) + rotating refresh token (30 d)
  client -> /mcp with Bearer mgo_...  (audience-checked against this gateway's MCP URL)

Tokens are opaque, stored as HMAC hashes like API keys, bound to one user and
one resource, revocable from the dashboard. A replayed (already rotated)
refresh token revokes its whole token family.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, update

from gateway.api import CurrentUser, gw
from gateway.db import utcnow
from gateway.oauth_store import ACCESS_PREFIX, ACCESS_TTL, CODE_TTL, REFRESH_PREFIX, REFRESH_TTL, SCOPE, OAuthClient, OAuthCode, OAuthToken
from gateway.security import hash_api_key, sign_token, verify_token
from gateway.ssrf import UnsafeURL, check_url

router = APIRouter()


# --- helpers ---------------------------------------------------------------------


def issuer(request: Request) -> str:
    return gw(request).settings.public_url.rstrip("/")


def resource_uri(request: Request) -> str:
    return f"{issuer(request)}/mcp"


def _same_resource(given: str | None, canonical: str) -> bool:
    if not given:
        return True  # clients MUST send it; we still bind the token to our canonical URI
    a, b = urlsplit(given.rstrip("/")), urlsplit(canonical)
    return (a.scheme.lower(), (a.netloc or "").lower(), a.path or "") in {(b.scheme, b.netloc, b.path), (b.scheme, b.netloc, "")}


def _is_loopback(url: str) -> bool:
    return urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")


def _valid_redirect(uri: str) -> bool:
    parts = urlsplit(uri)
    if parts.fragment or not parts.scheme:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme == "http":
        return _is_loopback(uri)
    return parts.scheme not in ("javascript", "data", "file") and "." in parts.scheme  # private-use URI schemes (RFC 8252)


def _redirect_allowed(uri: str, registered: list[str]) -> bool:
    if uri in registered:
        return True
    # RFC 8252 section 7.3: loopback redirects may use any port.
    p = urlsplit(uri)
    return _is_loopback(uri) and any(
        _is_loopback(r) and urlsplit(r).scheme == p.scheme and urlsplit(r).path == p.path for r in registered
    )


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not 43 <= len(verifier) <= 128:
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return secrets.compare_digest(digest, challenge)


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status, headers={"Cache-Control": "no-store"})


async def get_client(request: Request, client_id: str) -> OAuthClient | None:
    g = gw(request)
    async with g.db.session() as s:
        client = await s.get(OAuthClient, client_id)
    if client is not None or not client_id.startswith("https://"):
        return client
    # Client ID Metadata Document: the client_id is an https URL serving the client's metadata.
    try:
        await check_url(client_id, [], False)  # never fetch internal addresses on a client's say-so
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as http:
            resp = await http.get(client_id, headers={"Accept": "application/json"})
        doc = resp.json() if resp.status_code == 200 and len(resp.content) < 64_000 else None
    except (UnsafeURL, httpx.HTTPError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("client_id") != client_id:
        return None
    uris = [u for u in doc.get("redirect_uris", []) if isinstance(u, str) and _valid_redirect(u)]
    if not uris:
        return None
    client = OAuthClient(client_id=client_id, name=str(doc.get("client_name") or urlsplit(client_id).netloc)[:200], redirect_uris=uris, kind="cimd")
    async with g.db.session() as s:
        await s.merge(client)
        await s.commit()
    return client


async def _issue(request: Request, *, user_id: int, client: OAuthClient, family: str) -> dict[str, Any]:
    g = gw(request)
    access, refresh = ACCESS_PREFIX + secrets.token_urlsafe(32), REFRESH_PREFIX + secrets.token_urlsafe(32)
    now = utcnow()
    async with g.db.session() as s:
        for kind, token, ttl in (("access", access, ACCESS_TTL), ("refresh", refresh, REFRESH_TTL)):
            s.add(OAuthToken(kind=kind, token_hash=hash_api_key(token, g.settings.api_key_pepper), family=family, user_id=user_id,
                             client_id=client.client_id, client_name=client.name, resource=resource_uri(request),
                             expires_at=now + timedelta(seconds=ttl)))
        await s.commit()
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL, "refresh_token": refresh, "scope": SCOPE}


# --- discovery -------------------------------------------------------------------------


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata(request: Request) -> JSONResponse:
    return JSONResponse({
        "resource": resource_uri(request),
        "authorization_servers": [issuer(request)],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Team MCP gateway",
    })


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/openid-configuration")
async def authorization_server_metadata(request: Request) -> JSONResponse:
    iss = issuer(request)
    return JSONResponse({
        "issuer": iss,
        "authorization_endpoint": f"{iss}/oauth/authorize",
        "token_endpoint": f"{iss}/oauth/token",
        "registration_endpoint": f"{iss}/oauth/register",
        "revocation_endpoint": f"{iss}/oauth/revoke",
        "scopes_supported": [SCOPE],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    })


# --- registration (RFC 7591) ----------------------------------------------------------------


@router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return _oauth_error("invalid_client_metadata", "body must be JSON")
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and _valid_redirect(u) for u in uris):
        return _oauth_error("invalid_redirect_uri", "redirect_uris must be https, loopback http, or a private-use scheme")
    if body.get("token_endpoint_auth_method", "none") != "none":
        return _oauth_error("invalid_client_metadata", "only public clients (token_endpoint_auth_method=none) are supported")
    client = OAuthClient(client_id="mgc_" + secrets.token_urlsafe(16), name=str(body.get("client_name") or "MCP client")[:200], redirect_uris=uris, kind="dcr")
    async with gw(request).db.session() as s:
        s.add(client)
        await s.commit()
    return JSONResponse({
        "client_id": client.client_id, "client_id_issued_at": int(time.time()), "client_name": client.name,
        "redirect_uris": uris, "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


# --- authorization ------------------------------------------------------------------------------


@router.get("/oauth/authorize")
async def authorize(request: Request) -> Any:
    q = request.query_params
    client = await get_client(request, q.get("client_id", ""))
    redirect_uri = q.get("redirect_uri", "")
    # Never redirect to an unverified URI: errors before this point are shown, not sent.
    if client is None:
        raise HTTPException(400, "unknown client_id")
    if not redirect_uri and len(client.redirect_uris) == 1:
        redirect_uri = client.redirect_uris[0]
    if not _redirect_allowed(redirect_uri, client.redirect_uris):
        raise HTTPException(400, "redirect_uri is not registered for this client")

    def fail(error: str, description: str) -> RedirectResponse:
        params = {"error": error, "error_description": description, "iss": issuer(request)}
        if q.get("state"):
            params["state"] = q["state"]
        return RedirectResponse(f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urlencode(params)}", status_code=302)

    if q.get("response_type") != "code":
        return fail("unsupported_response_type", "only response_type=code is supported")
    if not q.get("code_challenge") or q.get("code_challenge_method") != "S256":
        return fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
    if not _same_resource(q.get("resource"), resource_uri(request)):
        return fail("invalid_target", f"this server only issues tokens for {resource_uri(request)}")
    req = sign_token("o", {
        "client_id": client.client_id, "redirect_uri": redirect_uri, "state": q.get("state"),
        "code_challenge": q["code_challenge"], "scope": SCOPE,
    }, gw(request).settings.session_secret, ttl_seconds=600)
    return RedirectResponse(f"/consent?{urlencode({'req': req})}", status_code=302)


def _pending(request: Request, req: str) -> dict[str, Any]:
    payload = verify_token(req, "o", gw(request).settings.session_secret)
    if not payload:
        raise HTTPException(400, "authorization request expired; start again from your MCP client")
    return payload


@router.get("/api/oauth/request")
async def consent_info(req: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    p = _pending(request, req)
    client = await get_client(request, p["client_id"])
    if client is None:
        raise HTTPException(400, "unknown client")
    return {
        "client_name": client.name, "client_id": client.client_id, "client_kind": client.kind,
        "redirect_host": urlsplit(p["redirect_uri"]).netloc or p["redirect_uri"], "scope": p["scope"],
        "resource": resource_uri(request), "user": {"email": user.email, "role": user.role},
    }


class Decision(BaseModel):
    req: str
    approve: bool


@router.post("/api/oauth/decision")
async def consent_decision(body: Decision, request: Request, user: CurrentUser) -> dict[str, str]:
    p = _pending(request, body.req)
    params: dict[str, str] = {"iss": issuer(request)}
    if p.get("state"):
        params["state"] = p["state"]
    if body.approve:
        code = secrets.token_urlsafe(32)
        async with gw(request).db.session() as s:
            s.add(OAuthCode(code_hash=hashlib.sha256(code.encode()).hexdigest(), client_id=p["client_id"], user_id=user.id,
                            redirect_uri=p["redirect_uri"], code_challenge=p["code_challenge"], resource=resource_uri(request),
                            expires_at=utcnow() + timedelta(seconds=CODE_TTL)))
            await s.commit()
        params["code"] = code
    else:
        params.update(error="access_denied", error_description="the user denied access")
    sep = "&" if "?" in p["redirect_uri"] else "?"
    return {"redirect": f"{p['redirect_uri']}{sep}{urlencode(params)}"}


# --- token ------------------------------------------------------------------------------------


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    form = await request.form()
    grant = form.get("grant_type")
    g = gw(request)
    client = await get_client(request, str(form.get("client_id") or ""))
    if client is None:
        return _oauth_error("invalid_client", "unknown client_id", 401)
    if not _same_resource(str(form.get("resource") or "") or None, resource_uri(request)):
        return _oauth_error("invalid_target", f"tokens are only issued for {resource_uri(request)}")

    if grant == "authorization_code":
        code_hash = hashlib.sha256(str(form.get("code") or "").encode()).hexdigest()
        async with g.db.session() as s:
            code = await s.scalar(select(OAuthCode).where(OAuthCode.code_hash == code_hash))
            if code is None or code.used_at is not None or code.expires_at < utcnow() or code.client_id != client.client_id:
                return _oauth_error("invalid_grant", "authorization code is invalid, expired or already used")
            if str(form.get("redirect_uri") or code.redirect_uri) != code.redirect_uri:
                return _oauth_error("invalid_grant", "redirect_uri does not match the authorization request")
            if not _pkce_ok(str(form.get("code_verifier") or ""), code.code_challenge):
                return _oauth_error("invalid_grant", "PKCE verification failed")
            code.used_at = utcnow()
            user_id = code.user_id
            await s.commit()
        tokens = await _issue(request, user_id=user_id, client=client, family=secrets.token_hex(16))
        return JSONResponse(tokens, headers={"Cache-Control": "no-store"})

    if grant == "refresh_token":
        token_hash = hash_api_key(str(form.get("refresh_token") or ""), g.settings.api_key_pepper)
        async with g.db.session() as s:
            old = await s.scalar(select(OAuthToken).where(OAuthToken.token_hash == token_hash, OAuthToken.kind == "refresh"))
            if old is None or old.client_id != client.client_id:
                return _oauth_error("invalid_grant", "unknown refresh token")
            if old.revoked_at is not None:
                # Reuse of a rotated token means it leaked: revoke the whole grant.
                await s.execute(update(OAuthToken).where(OAuthToken.family == old.family, OAuthToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
                await s.commit()
                return _oauth_error("invalid_grant", "refresh token was already used; the grant has been revoked")
            if old.expires_at < utcnow():
                return _oauth_error("invalid_grant", "refresh token expired")
            await s.execute(update(OAuthToken).where(OAuthToken.family == old.family, OAuthToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
            user_id, family = old.user_id, old.family
            await s.commit()
        tokens = await _issue(request, user_id=user_id, client=client, family=family)
        return JSONResponse(tokens, headers={"Cache-Control": "no-store"})

    return _oauth_error("unsupported_grant_type", "use authorization_code or refresh_token")


@router.post("/oauth/revoke")
async def revoke(request: Request) -> JSONResponse:
    form = await request.form()
    token_hash = hash_api_key(str(form.get("token") or ""), gw(request).settings.api_key_pepper)
    async with gw(request).db.session() as s:
        row = await s.scalar(select(OAuthToken).where(OAuthToken.token_hash == token_hash))
        if row is not None:
            await s.execute(update(OAuthToken).where(OAuthToken.family == row.family, OAuthToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
            await s.commit()
    return JSONResponse({}, status_code=200)  # RFC 7009: same answer whether or not the token existed


# --- connected apps in the dashboard ---------------------------------------------------------------


@router.get("/api/oauth/grants")
async def list_grants(request: Request, user: CurrentUser, all: bool = False) -> list[dict[str, Any]]:
    async with gw(request).db.session() as s:
        q = select(OAuthToken).where(OAuthToken.kind == "refresh").order_by(OAuthToken.id.desc())
        if not (all and user.role == "admin"):
            q = q.where(OAuthToken.user_id == user.id)
        rows = (await s.scalars(q)).all()
        access = (await s.scalars(select(OAuthToken).where(OAuthToken.kind == "access", OAuthToken.family.in_([r.family for r in rows] or [""])))).all()
    last_used: dict[str, Any] = {}
    for a in access:
        if a.last_used_at and (a.family not in last_used or a.last_used_at > last_used[a.family]):
            last_used[a.family] = a.last_used_at
    grants: dict[str, dict[str, Any]] = {}
    for r in rows:  # newest refresh token per family describes the grant
        if r.family in grants:
            continue
        grants[r.family] = {"family": r.family, "client_name": r.client_name, "client_id": r.client_id, "user_id": r.user_id,
                            "created_at": r.created_at, "last_used_at": last_used.get(r.family), "active": r.revoked_at is None and r.expires_at > utcnow()}
    return list(grants.values())


@router.post("/api/oauth/grants/{family}/revoke")
async def revoke_grant(family: str, request: Request, user: CurrentUser) -> dict[str, bool]:
    async with gw(request).db.session() as s:
        owner = await s.scalar(select(OAuthToken.user_id).where(OAuthToken.family == family).limit(1))
        if owner is None or (owner != user.id and user.role != "admin"):
            raise HTTPException(404, "no such grant")
        await s.execute(update(OAuthToken).where(OAuthToken.family == family, OAuthToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
        await s.commit()
    return {"ok": True}
