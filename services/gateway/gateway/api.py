"""REST API for the dashboard. Sessions are signed HttpOnly cookies; credentials never leave the server."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select

from gateway import audit
from gateway.audit import Principal
from gateway.chat import ChatError, run_chat
from gateway.db import ApiKey, AuditLog, Invite, UpstreamServer, User, utcnow
from gateway.security import hash_api_key, hash_password, hash_token, new_api_key, sign_token, verify_password, verify_token
from gateway.ssrf import UnsafeURL, check_url
from gateway.mcp_endpoint import Gateway
from gateway.upstreams import SLUG_RE, as_gateway_tool

router = APIRouter(prefix="/api")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
Email = Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)]
COOKIE = "mgw_session"
SESSION_TTL = 12 * 3600


def gw(request: Request) -> Any:
    return request.app.state.gateway


# --- sessions ---------------------------------------------------------------


async def current_user(request: Request) -> User:
    token = request.cookies.get(COOKIE)
    payload = verify_token(token, "s", gw(request).settings.session_secret) if token else None
    if not payload:
        raise HTTPException(401, "not signed in")
    async with gw(request).db.session() as s:
        user = await s.get(User, int(payload["uid"]))
    if user is None or not user.active:
        raise HTTPException(401, "not signed in")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "dashboard":
        # Cross-site forms cannot set custom headers, and no CORS is allowed: CSRF guard.
        raise HTTPException(403, "missing X-Requested-With header")
    return user


async def admin_user(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admins only")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
AdminUser = Annotated[User, Depends(admin_user)]


def _set_session(request: Request, response: Response, user: User) -> None:
    settings = gw(request).settings
    response.set_cookie(
        COOKIE, sign_token("s", {"uid": user.id}, settings.session_secret, SESSION_TTL),
        max_age=SESSION_TTL, httponly=True, samesite="lax", secure=settings.cookie_secure, path="/",
    )


def _me(user: User) -> dict[str, Any]:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role}


class LoginIn(BaseModel):
    email: Email
    password: str


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    async with gw(request).db.session() as s:
        user = await s.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not user.active or not verify_password(body.password, user.password_hash):
        await asyncio.sleep(0.3)
        raise HTTPException(401, "wrong email or password")
    _set_session(request, response, user)
    return _me(user)


@router.post("/auth/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentUser) -> dict[str, Any]:
    return _me(user)


# --- invites and members ----------------------------------------------------


class InviteIn(BaseModel):
    email: Email
    role: Literal["admin", "member"] = "member"


@router.post("/invites")
async def create_invite(body: InviteIn, request: Request, admin: AdminUser) -> dict[str, Any]:
    token = secrets.token_urlsafe(24)
    async with gw(request).db.session() as s:
        if await s.scalar(select(User).where(User.email == body.email.lower())):
            raise HTTPException(409, "a user with that email already exists")
        invite = Invite(email=body.email.lower(), role=body.role, token_hash=hash_token(token), invited_by=admin.id, expires_at=utcnow() + timedelta(days=7))
        s.add(invite)
        await s.commit()
    # No mail server: the admin shares the link. The token is shown once and stored hashed.
    return {"id": invite.id, "email": invite.email, "role": invite.role, "link": f"{gw(request).settings.public_url}/accept?token={token}", "expires_at": invite.expires_at}


@router.get("/invites")
async def list_invites(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
    async with gw(request).db.session() as s:
        rows = (await s.scalars(select(Invite).where(Invite.accepted_at.is_(None)).order_by(Invite.id.desc()))).all()
    return [{"id": i.id, "email": i.email, "role": i.role, "expires_at": i.expires_at, "expired": i.expires_at < utcnow()} for i in rows]


@router.delete("/invites/{invite_id}")
async def delete_invite(invite_id: int, request: Request, admin: AdminUser) -> dict[str, bool]:
    async with gw(request).db.session() as s:
        await s.execute(delete(Invite).where(Invite.id == invite_id))
        await s.commit()
    return {"ok": True}


@router.get("/invites/lookup")
async def lookup_invite(token: str, request: Request) -> dict[str, Any]:
    async with gw(request).db.session() as s:
        invite = await s.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
    if invite is None or invite.accepted_at or invite.expires_at < utcnow():
        raise HTTPException(404, "invite not found or expired")
    return {"email": invite.email, "role": invite.role}


class AcceptIn(BaseModel):
    token: str
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=10, max_length=200)


@router.post("/invites/accept")
async def accept_invite(body: AcceptIn, request: Request, response: Response) -> dict[str, Any]:
    async with gw(request).db.session() as s:
        invite = await s.scalar(select(Invite).where(Invite.token_hash == hash_token(body.token)).with_for_update())
        if invite is None or invite.accepted_at or invite.expires_at < utcnow():
            raise HTTPException(404, "invite not found or expired")
        if await s.scalar(select(User).where(User.email == invite.email)):
            raise HTTPException(409, "already accepted")
        user = User(email=invite.email, name=body.name, role=invite.role, password_hash=hash_password(body.password))
        s.add(user)
        invite.accepted_at = utcnow()
        await s.commit()
    _set_session(request, response, user)
    return _me(user)


@router.get("/members")
async def list_members(request: Request, admin: AdminUser) -> list[dict[str, Any]]:
    async with gw(request).db.session() as s:
        users = (await s.scalars(select(User).order_by(User.id))).all()
        keys = (await s.scalars(select(ApiKey).where(ApiKey.revoked_at.is_(None)))).all()
    active_keys: dict[int, int] = {}
    for k in keys:
        active_keys[k.user_id] = active_keys.get(k.user_id, 0) + 1
    return [{**_me(u), "active": u.active, "created_at": u.created_at, "active_keys": active_keys.get(u.id, 0)} for u in users]


class MemberPatch(BaseModel):
    role: Literal["admin", "member"] | None = None
    active: bool | None = None


@router.patch("/members/{user_id}")
async def update_member(user_id: int, body: MemberPatch, request: Request, admin: AdminUser) -> dict[str, Any]:
    if user_id == admin.id and (body.role == "member" or body.active is False):
        raise HTTPException(400, "you cannot demote or deactivate yourself")
    async with gw(request).db.session() as s:
        user = await s.get(User, user_id)
        if user is None:
            raise HTTPException(404, "no such user")
        if body.role:
            user.role = body.role
        if body.active is not None:
            user.active = body.active
        await s.commit()
    return _me(user)


# --- API keys ---------------------------------------------------------------


def _key_out(k: ApiKey, email: str | None = None) -> dict[str, Any]:
    return {
        "id": k.id, "name": k.name, "prefix": k.prefix, "user_id": k.user_id, "user_email": email,
        "created_at": k.created_at, "last_used_at": k.last_used_at, "revoked_at": k.revoked_at,
    }


@router.get("/keys")
async def list_keys(request: Request, user: CurrentUser, all: bool = False) -> list[dict[str, Any]]:
    async with gw(request).db.session() as s:
        q = select(ApiKey, User.email).join(User).order_by(ApiKey.id.desc())
        if not (all and user.role == "admin"):
            q = q.where(ApiKey.user_id == user.id)
        rows = (await s.execute(q)).all()
    return [_key_out(k, email) for k, email in rows]


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    user_id: int | None = None  # admins may issue a key for another member


@router.post("/keys")
async def create_key(body: KeyIn, request: Request, user: CurrentUser) -> dict[str, Any]:
    owner_id = body.user_id or user.id
    if owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "only admins can issue keys for others")
    key = new_api_key()
    async with gw(request).db.session() as s:
        owner = await s.get(User, owner_id)
        if owner is None or not owner.active:
            raise HTTPException(404, "no such active user")
        row = ApiKey(user_id=owner_id, name=body.name, prefix=key[:12], key_hash=hash_api_key(key, gw(request).settings.api_key_pepper), created_by=user.id)
        s.add(row)
        await s.commit()
    return {**_key_out(row, owner.email), "key": key}  # the only time the key is ever returned


@router.post("/keys/{key_id}/revoke")
async def revoke_key(key_id: int, request: Request, user: CurrentUser) -> dict[str, Any]:
    async with gw(request).db.session() as s:
        key = await s.get(ApiKey, key_id)
        if key is None or (key.user_id != user.id and user.role != "admin"):
            raise HTTPException(404, "no such key")
        if key.revoked_at is None:
            key.revoked_at = utcnow()
            await s.commit()
    return _key_out(key)


# --- upstream servers ---------------------------------------------------------


class ServerIn(BaseModel):
    slug: str
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=8, max_length=2000)
    auth_type: Literal["none", "bearer", "header"] = "none"
    auth_header: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9-]+$")
    credential: str | None = Field(default=None, max_length=4000)  # write-only
    enabled: bool = True
    timeout_seconds: float = Field(default=20, ge=1, le=120)
    admin_only_tools: list[str] = []

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug: lowercase letters, digits and single hyphens, 1-24 characters, starting with a letter")
        return v


class ServerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=8, max_length=2000)
    auth_type: Literal["none", "bearer", "header"] | None = None
    auth_header: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9-]+$")
    credential: str | None = Field(default=None, max_length=4000)  # empty/absent keeps the stored one
    enabled: bool | None = None
    timeout_seconds: float | None = Field(default=None, ge=1, le=120)
    admin_only_tools: list[str] | None = None


class HostedIn(BaseModel):
    """An MCP server the platform runs from a container image (Streamable HTTP inside the container)."""

    slug: str
    name: str = Field(min_length=1, max_length=200)
    image: str = Field(min_length=3, max_length=500, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
    port: int = Field(default=8000, ge=1024, le=65535)
    path: str = Field(default="/mcp", max_length=200, pattern=r"^/[A-Za-z0-9._/-]*$")
    env: dict[str, str] = Field(default_factory=dict)  # becomes a Secret in the server's namespace
    allow_egress: bool = False  # outbound HTTPS to the internet (never to private ranges)
    timeout_seconds: float = Field(default=20, ge=1, le=120)
    admin_only_tools: list[str] = []

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v) or len(v) > 20:
            raise ValueError("slug: lowercase letters, digits and single hyphens, 1-20 characters, starting with a letter")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not ENV_NAME.match(k):
                raise ValueError(f"invalid environment variable name {k!r}")
        return v


class HostedPatch(BaseModel):
    image: str | None = Field(default=None, min_length=3, max_length=500, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
    env: dict[str, str] | None = None
    allow_egress: bool | None = None


def _server_out(server: UpstreamServer, user: User) -> dict[str, Any]:
    principal = Principal(user.id, user.email, user.role, "mcp")
    tools = []
    for e in server.tools or []:
        admin_only = e["upstream"] in (server.admin_only_tools or [])
        if not Gateway.allowed(principal, server, e["upstream"]):
            continue
        t = as_gateway_tool(server.slug, server.name, e)
        tools.append({"name": t.name, "upstream": e["upstream"], "description": e["definition"].get("description", ""), "admin_only": admin_only, "read_only": bool((e["definition"].get("annotations") or {}).get("readOnlyHint"))})
    status, reason = ("disabled", None) if not server.enabled else (server.status, server.status_reason)
    if server.hosted_state in ("deploying", "failed", "deleting"):
        status, reason = server.hosted_state, server.hosted_reason
    out = {
        "id": server.id, "slug": server.slug, "name": server.name, "enabled": server.enabled,
        "status": status, "status_reason": reason,
        "checked_at": server.checked_at, "tools": tools, "managed_by": server.managed_by,
    }
    if principal.is_admin:  # members do not need to know where upstreams live
        out.update(
            url=server.url, auth_type=server.auth_type, auth_header=server.auth_header,
            has_credential=bool(server.credential_enc), timeout_seconds=server.timeout_seconds,
            admin_only_tools=server.admin_only_tools or [],
        )
        if server.hosted_image:
            out["hosted"] = {"image": server.hosted_image, "port": server.hosted_port, "path": server.hosted_path,
                             "has_env": bool(server.hosted_env_enc), "allow_egress": bool(server.hosted_allow_egress),
                             "namespace": f"mcp-{server.slug}"}
    return out


async def _check(request: Request, url: str) -> None:
    g = gw(request)
    try:
        await check_url(url, g.allowed_internal, g.settings.ssrf_allow_all)
    except UnsafeURL as exc:
        raise HTTPException(422, f"URL refused: {exc}") from None


@router.get("/servers")
async def list_servers(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
    async with gw(request).db.session() as s:
        servers = (await s.scalars(select(UpstreamServer).order_by(UpstreamServer.id))).all()
    return [_server_out(x, user) for x in servers if user.role == "admin" or x.enabled]


@router.post("/servers")
async def create_server(body: ServerIn, request: Request, admin: AdminUser) -> dict[str, Any]:
    await _check(request, body.url)
    g = gw(request)
    async with g.db.session() as s:
        if await s.scalar(select(UpstreamServer).where(UpstreamServer.slug == body.slug)):
            raise HTTPException(409, "slug already in use")
        server = UpstreamServer(
            slug=body.slug, name=body.name, url=body.url, auth_type=body.auth_type, auth_header=body.auth_header,
            credential_enc=g.box.seal(body.credential) if body.credential and body.auth_type != "none" else None,
            enabled=body.enabled, timeout_seconds=body.timeout_seconds, admin_only_tools=body.admin_only_tools,
        )
        if body.auth_type == "header" and not body.auth_header:
            raise HTTPException(422, "auth_header is required for header credentials")
        s.add(server)
        await s.commit()
    await g.monitor.probe(server.id)
    return await _reload(request, server.id, admin)


@router.post("/servers/hosted")
async def create_hosted_server(body: HostedIn, request: Request, admin: AdminUser) -> dict[str, Any]:
    g = gw(request)
    if not g.settings.provisioner_enabled:
        raise HTTPException(400, "hosted servers need the provisioner (gateway.provisioner.enabled)")
    async with g.db.session() as s:
        if await s.scalar(select(UpstreamServer).where(UpstreamServer.slug == body.slug)):
            raise HTTPException(409, "slug already in use")
        server = UpstreamServer(
            slug=body.slug, name=body.name, url=f"http://{body.slug}.mcp-{body.slug}.svc.cluster.local:{body.port}{body.path}",
            auth_type="none", enabled=True, timeout_seconds=body.timeout_seconds, admin_only_tools=body.admin_only_tools,
            managed_by="hosted", hosted_image=body.image, hosted_port=body.port, hosted_path=body.path,
            hosted_env_enc=g.box.seal(json.dumps(body.env)) if body.env else None, hosted_allow_egress=body.allow_egress,
            hosted_state="deploying", status="unknown",
        )
        s.add(server)
        await s.commit()
    return await _reload(request, server.id, admin)


@router.patch("/servers/{server_id}/hosted")
async def update_hosted_server(server_id: int, body: HostedPatch, request: Request, admin: AdminUser) -> dict[str, Any]:
    g = gw(request)
    async with g.db.session() as s:
        server = await s.get(UpstreamServer, server_id)
        if server is None or not server.hosted_image:
            raise HTTPException(404, "no such hosted server")
        if server.hosted_state == "deleting":
            raise HTTPException(409, "server is being removed")
        if body.image:
            server.hosted_image = body.image
        if body.env is not None:
            server.hosted_env_enc = g.box.seal(json.dumps(body.env)) if body.env else None
        if body.allow_egress is not None:
            server.hosted_allow_egress = body.allow_egress
        server.hosted_state, server.hosted_reason, server.status = "deploying", None, "unknown"
        await s.commit()
    return await _reload(request, server_id, admin)


@router.patch("/servers/{server_id}")
async def update_server(server_id: int, body: ServerPatch, request: Request, admin: AdminUser) -> dict[str, Any]:
    g = gw(request)
    if body.url:
        await _check(request, body.url)
    async with g.db.session() as s:
        server = await s.get(UpstreamServer, server_id)
        if server is None:
            raise HTTPException(404, "no such server")
        if server.hosted_image and (body.url or body.auth_type):
            raise HTTPException(409, "a hosted server's URL is managed by the platform")
        fields = body.model_dump(exclude_unset=True, exclude={"credential"})
        for k, v in fields.items():
            if v is not None or k == "auth_header":
                setattr(server, k, v)
        if body.credential:
            server.credential_enc = g.box.seal(body.credential)
        if server.auth_type == "none":
            server.credential_enc = None
        if server.auth_type == "header" and not server.auth_header:
            raise HTTPException(422, "auth_header is required for header credentials")
        if body.url or body.auth_type or body.credential or body.enabled:
            server.status = "unknown"
        await s.commit()
    if server.enabled:
        await g.monitor.probe(server_id)
    return await _reload(request, server_id, admin)


@router.delete("/servers/{server_id}")
async def delete_server(server_id: int, request: Request, admin: AdminUser) -> dict[str, bool]:
    async with gw(request).db.session() as s:
        server = await s.get(UpstreamServer, server_id)
        if server is None:
            raise HTTPException(404, "no such server")
        if server.managed_by and server.managed_by.startswith("store:"):
            raise HTTPException(409, "this server belongs to a provisioned store; delete the store instead")
        if server.hosted_image:
            # Tools disappear now; the provisioner removes the namespace, then the row.
            server.enabled, server.hosted_state, server.hosted_reason = False, "deleting", None
        else:
            await s.delete(server)
        await s.commit()
    return {"ok": True}


@router.post("/servers/{server_id}/refresh")
async def refresh_server(server_id: int, request: Request, admin: AdminUser) -> dict[str, Any]:
    await gw(request).monitor.probe(server_id)
    return await _reload(request, server_id, admin)


async def _reload(request: Request, server_id: int, user: User) -> dict[str, Any]:
    async with gw(request).db.session() as s:
        server = await s.get(UpstreamServer, server_id)
    if server is None:
        raise HTTPException(404, "no such server")
    return _server_out(server, user)


# --- audit log ----------------------------------------------------------------


@router.get("/audit")
async def audit_log(
    request: Request, user: CurrentUser,
    user_id: int | None = None, server: str | None = None, outcome: str | None = None,
    before_id: int | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 500))
    if user.role != "admin":
        q = q.where(AuditLog.user_id == user.id)  # members see their own calls
    elif user_id:
        q = q.where(AuditLog.user_id == user_id)
    if server:
        q = q.where(AuditLog.server_slug == server)
    if outcome:
        q = q.where(AuditLog.outcome == outcome)
    if before_id:
        q = q.where(AuditLog.id < before_id)
    async with gw(request).db.session() as s:
        rows = (await s.scalars(q)).all()
    return [
        {"id": r.id, "ts": r.ts, "user_id": r.user_id, "user_email": r.user_email, "source": r.source, "api_key_prefix": r.api_key_prefix,
         "server": r.server_slug, "tool": r.tool, "duration_ms": r.duration_ms, "outcome": r.outcome, "error": r.error}
        for r in rows
    ]


# --- chat, tools, connection info ---------------------------------------------


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


@router.post("/chat")
async def chat(body: ChatIn, request: Request, user: CurrentUser) -> dict[str, Any]:
    try:
        return await run_chat(gw(request).settings, user.id, [m.model_dump() for m in body.messages])
    except ChatError as exc:
        raise HTTPException(502, str(exc)) from None


@router.get("/tools")
async def my_tools(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
    principal = Principal(user.id, user.email, user.role, "mcp")
    return [{"name": as_gateway_tool(s.slug, s.name, e).name, "server": s.slug} for s, e in await gw(request).visible_tools(principal)]


@router.get("/connect")
async def connect_info(request: Request, user: CurrentUser) -> dict[str, Any]:
    url = f"{gw(request).settings.public_url.rstrip('/')}/mcp"
    return {"mcp_url": url, "llm_model": gw(request).settings.llm_model, "chat_enabled": bool(gw(request).settings.llm_api_key)}


@router.get("/stats")
async def stats(request: Request, user: CurrentUser) -> dict[str, Any]:
    from sqlalchemy import func

    async with gw(request).db.session() as s:
        since = utcnow() - timedelta(hours=24)
        q = select(AuditLog.server_slug, AuditLog.outcome, func.count(), func.avg(AuditLog.duration_ms)).where(AuditLog.ts >= since).group_by(AuditLog.server_slug, AuditLog.outcome)
        if user.role != "admin":
            q = q.where(AuditLog.user_id == user.id)
        rows = (await s.execute(q)).all()
    per: dict[str, dict[str, Any]] = {}
    for slug, outcome, count, avg in rows:
        d = per.setdefault(slug or "-", {"server": slug or "-", "calls": 0, "errors": 0, "avg_ms": 0.0})
        d["avg_ms"] = (d["avg_ms"] * d["calls"] + float(avg or 0) * count) / (d["calls"] + count)
        d["calls"] += count
        if outcome != "ok":
            d["errors"] += count
    return {"window_hours": 24, "servers": sorted(per.values(), key=lambda d: -d["calls"])}
