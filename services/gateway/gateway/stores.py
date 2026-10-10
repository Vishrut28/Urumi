"""Store provisioning API. The gateway only records intent; the provisioner deployment does the work.

POST is safe to retry: the same user asking for the same slug gets the existing
store back (a failed one is re-queued), never a duplicate.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from gateway.api import CurrentUser, gw
from gateway.db import Store, UpstreamServer, User, utcnow

router = APIRouter(prefix="/api/stores")

SLUG = re.compile(r"^[a-z][a-z0-9]{1,11}$")
RESERVED = {"main", "app", "shop", "www", "api", "admin", "mail", "mcp"}
ACTIVE = ("provisioning", "ready", "deleting")


def store_server_slug(store_slug: str) -> str:
    return f"shop-{store_slug}"


def _out(store: Store, owner_email: str | None, server: UpstreamServer | None) -> dict[str, Any]:
    return {
        "id": store.id, "slug": store.slug, "name": store.name, "status": store.status,
        "status_reason": store.status_reason, "step": store.step, "url": store.url, "namespace": store.namespace,
        "created_at": store.created_at, "updated_at": store.updated_at, "owner_id": store.owner_id, "owner_email": owner_email,
        "mcp_server": server.slug if server else None, "mcp_status": server.status if server else None,
    }


@router.get("")
async def list_stores(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
    async with gw(request).db.session() as s:
        q = select(Store, User.email).join(User, User.id == Store.owner_id).order_by(Store.id.desc())
        if user.role != "admin":
            q = q.where(Store.owner_id == user.id)
        rows = (await s.execute(q)).all()
        servers = {x.managed_by: x for x in (await s.scalars(select(UpstreamServer).where(UpstreamServer.managed_by.like("store:%")))).all()}
    return [_out(st, email, servers.get(f"store:{st.id}")) for st, email in rows]


class StoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError("slug: 2-12 lowercase letters or digits, starting with a letter")
        if v in RESERVED:
            raise ValueError(f"slug '{v}' is reserved")
        return v


@router.post("")
async def create_store(body: StoreIn, request: Request, user: CurrentUser) -> dict[str, Any]:
    settings = gw(request).settings
    async with gw(request).db.session() as s:
        existing = await s.scalar(select(Store).where(Store.slug == body.slug).with_for_update())
        if existing is not None:
            if existing.owner_id != user.id:
                raise HTTPException(409, "that store name is taken")
            if existing.status == "deleting":
                raise HTTPException(409, "that store is being deleted; try again when it is gone")
            if existing.status == "failed":  # retry
                existing.status, existing.status_reason, existing.step = "provisioning", None, None
                existing.deadline_at = utcnow() + timedelta(seconds=settings.provisioner_timeout_seconds)
                await s.commit()
            return _out(existing, user.email, None)  # idempotent: same request, same store
        count = await s.scalar(select(func.count()).select_from(Store).where(Store.owner_id == user.id, Store.status.in_(ACTIVE + ("failed",))))
        if (count or 0) >= settings.provisioner_max_stores_per_user:
            raise HTTPException(429, f"store limit reached: at most {settings.provisioner_max_stores_per_user} stores per user")
        store = Store(
            slug=body.slug, name=body.name, owner_id=user.id, status="provisioning",
            url=f"{settings.provisioner_store_scheme}://{body.slug}.{settings.provisioner_store_domain}",
            namespace=f"store-{body.slug}", deadline_at=utcnow() + timedelta(seconds=settings.provisioner_timeout_seconds), attempts=0,
        )
        s.add(store)
        try:
            await s.commit()
        except IntegrityError:  # a concurrent request for the same slug won the race
            raise HTTPException(409, "that store name was just taken; retry to get its status") from None
    return _out(store, user.email, None)


async def _owned(request: Request, store_id: int, user: User) -> Store:
    async with gw(request).db.session() as s:
        store = await s.get(Store, store_id)
    if store is None or (store.owner_id != user.id and user.role != "admin"):
        raise HTTPException(404, "no such store")
    return store


@router.delete("/{store_id}")
async def delete_store(store_id: int, request: Request, user: CurrentUser) -> dict[str, Any]:
    await _owned(request, store_id, user)
    async with gw(request).db.session() as s:
        store = await s.get(Store, store_id)
        assert store is not None
        store.status, store.status_reason, store.step = "deleting", None, None
        await s.commit()
    return {"ok": True}


@router.post("/{store_id}/retry")
async def retry_store(store_id: int, request: Request, user: CurrentUser) -> dict[str, Any]:
    await _owned(request, store_id, user)
    async with gw(request).db.session() as s:
        store = await s.get(Store, store_id)
        assert store is not None
        if store.status != "failed":
            raise HTTPException(409, "only a failed store can be retried")
        store.status, store.status_reason, store.step = "provisioning", None, None
        store.deadline_at = utcnow() + timedelta(seconds=gw(request).settings.provisioner_timeout_seconds)
        await s.commit()
    return {"ok": True}
