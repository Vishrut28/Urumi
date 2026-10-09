"""First-run state: the bootstrap admin and the upstream servers the chart ships with.

SEED_SERVERS is JSON from the Helm values, e.g.
  [{"slug": "woocommerce", "name": "WooCommerce", "url": "http://woo-mcp.store-main.svc.cluster.local:8000/mcp",
    "auth_type": "bearer", "credential_env": "UPSTREAM_WOOCOMMERCE_TOKEN", "admin_only_tools": ["create_product"]}]
The credential itself is never in the JSON: `credential_env` names an environment
variable that the chart fills from a Secret. A seeded server is created once;
after that the dashboard owns it, except that its credential follows the Secret
so rotating the Secret and restarting the gateway rotates the credential.
"""

from __future__ import annotations

import json
import logging
import os

from sqlalchemy import func, select

from gateway.config import Settings
from gateway.db import Database, UpstreamServer, User
from gateway.security import CredentialBox, hash_password

log = logging.getLogger("gateway.seed")


async def seed(settings: Settings, db: Database, box: CredentialBox) -> None:
    async with db.session() as s:
        if not await s.scalar(select(func.count()).select_from(User)):
            if not (settings.admin_email and settings.admin_password):
                raise RuntimeError("no users yet: set ADMIN_EMAIL and ADMIN_PASSWORD (from a Secret) for the first admin")
            s.add(User(email=settings.admin_email.lower(), name="Admin", role="admin", password_hash=hash_password(settings.admin_password)))
            log.info("created bootstrap admin %s", settings.admin_email)

        for spec in json.loads(settings.seed_servers or "[]"):
            credential = os.environ.get(spec["credential_env"], "") if spec.get("credential_env") else ""
            existing = await s.scalar(select(UpstreamServer).where(UpstreamServer.slug == spec["slug"]))
            if existing is None:
                s.add(
                    UpstreamServer(
                        slug=spec["slug"], name=spec["name"], url=spec["url"],
                        auth_type=spec.get("auth_type", "none"), auth_header=spec.get("auth_header"),
                        credential_enc=box.seal(credential) if credential else None,
                        timeout_seconds=float(spec.get("timeout_seconds", settings.default_call_timeout_seconds)),
                        admin_only_tools=spec.get("admin_only_tools", []), managed_by="seed",
                    )
                )
                log.info("registered seed upstream %s -> %s", spec["slug"], spec["url"])
            elif existing.managed_by == "seed" and credential and existing.auth_type != "none":
                if not existing.credential_enc or box.open(existing.credential_enc) != credential:
                    existing.credential_enc = box.seal(credential)
                    log.info("rotated credential of seed upstream %s from its Secret", spec["slug"])
        await s.commit()
