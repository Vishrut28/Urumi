"""Provisioner lifecycle with a fake Kubernetes API and a fake helm."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from gateway import provisioner as prov
from gateway.config import Settings
from gateway.db import Database, Store, UpstreamServer, User, utcnow
from gateway.security import CredentialBox


class FakeKube:
    def __init__(self):
        self.namespaces: set[str] = set()
        self.secrets: dict[tuple[str, str], dict[str, str]] = {}
        self.applied: list[dict] = []

    async def ensure_namespace(self, name, labels):
        assert name.startswith(("store-", "mcp-"))
        self.namespaces.add(name)

    async def ensure_secret(self, ns, name, data, labels=None):
        return self.secrets.setdefault((ns, name), dict(data))

    async def replace_secret(self, ns, name, data, labels=None):
        self.secrets[(ns, name)] = dict(data)

    async def delete_namespace(self, name, wait_seconds=300):
        self.namespaces.discard(name)
        self.secrets = {k: v for k, v in self.secrets.items() if k[0] != name}

    async def apply(self, obj):
        self.applied.append(obj)

    async def deployment_ready(self, ns, name):
        return True, "available"


class FakeHelm:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.release_state: dict[str, str] = {}
        self.fail_install = False

    async def __call__(self, *args, check=True):
        self.calls.append(args)
        verb, release = args[0], args[1] if args[0] != "upgrade" else args[2]
        if verb == "status":
            state = self.release_state.get(release)
            return prov._Result(0 if state else 1, json.dumps({"info": {"status": state}}) if state else "", "" if state else "Error: release: not found")
        if verb == "uninstall":
            self.release_state.pop(release, None)
            return prov._Result(0, "", "")
        if verb == "upgrade":
            if self.fail_install:
                if check:
                    raise RuntimeError("helm upgrade failed: timed out waiting for the condition")
            self.release_state[release] = "deployed"
            return prov._Result(0, "", "")
        raise AssertionError(args)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'p.db'}", session_secret="s" * 32, api_key_pepper="p" * 32,
                        credentials_key=Fernet.generate_key().decode(), provisioner_timeout_seconds=600, ssrf_allow_all=True)
    db = Database(settings.database_url)
    await db.create_all()
    async with db.session() as s:
        s.add(User(id=1, email="owner@example.com", role="member"))
        await s.commit()
    kube, helm = FakeKube(), FakeHelm()
    monkeypatch.setattr(prov, "_helm", helm)
    p = prov.Provisioner(settings, db, CredentialBox(settings.credentials_key), kube)

    async def probe(server_id):  # the gateway reaching the new woo-mcp
        async with db.session() as s:
            server = await s.get(UpstreamServer, server_id)
            server.status = "connected"
            await s.commit()

    monkeypatch.setattr(p.monitor, "probe", probe)
    yield settings, db, kube, helm, p
    await db.dispose()


async def add_store(db, slug="berlin", deadline=None):
    async with db.session() as s:
        st = Store(slug=slug, name=slug.title(), owner_id=1, status="provisioning", url=f"http://{slug}.localtest.me",
                   namespace=f"store-{slug}", deadline_at=deadline or utcnow() + timedelta(minutes=10), attempts=0)
        s.add(st)
        await s.commit()
        return st.id


async def test_store_provisions_and_registers_namespaced_mcp_server(env):
    _, db, kube, helm, p = env
    sid = await add_store(db)
    await p.reconcile_store(sid)
    async with db.session() as s:
        store = await s.get(Store, sid)
        server = await s.scalar(select(UpstreamServer).where(UpstreamServer.managed_by == f"store:{sid}"))
    assert store.status == "ready" and store.step == "done"
    assert server.slug == "shop-berlin" and server.url == "http://woo-mcp.store-berlin.svc.cluster.local:8000/mcp"
    assert server.owner_id == 1 and server.auth_type == "bearer"
    assert "store-berlin" in kube.namespaces and ("store-berlin", "store-secrets") in kube.secrets
    upgrade = next(c for c in helm.calls if c[0] == "upgrade")
    assert upgrade[2] == "store-berlin" and "--wait" in upgrade


async def test_restart_mid_install_recovers(env):
    """A provisioner killed during `helm install` leaves a pending release; the next run cleans it and completes."""
    _, db, kube, helm, p = env
    sid = await add_store(db)
    helm.release_state["store-berlin"] = "pending-install"
    kube.secrets[("store-berlin", "store-secrets")] = {"mcp-token": "kept-from-first-attempt"}
    await p.reconcile_store(sid)
    assert any(c[0] == "uninstall" for c in helm.calls)
    async with db.session() as s:
        assert (await s.get(Store, sid)).status == "ready"
        server = await s.scalar(select(UpstreamServer).where(UpstreamServer.slug == "shop-berlin"))
    assert p.box.open(server.credential_enc) == "kept-from-first-attempt"  # secrets are reused, not regenerated


async def test_past_deadline_fails_cleanly_with_reason(env):
    _, db, _, helm, p = env
    sid = await add_store(db, deadline=utcnow() - timedelta(seconds=1))
    await p.reconcile_store(sid)
    async with db.session() as s:
        store = await s.get(Store, sid)
    assert store.status == "failed" and "timed out" in store.status_reason


async def test_failed_helm_after_deadline_reports_step(env, monkeypatch):
    _, db, _, helm, p = env
    helm.fail_install = True
    sid = await add_store(db, deadline=utcnow() + timedelta(seconds=2))
    monkeypatch.setattr(prov.asyncio, "sleep", lambda *_: _noop())  # skip the retry back-off
    await p.reconcile_store(sid)  # transient failure before the deadline: stays provisioning
    async with db.session() as s:
        store = await s.get(Store, sid)
    assert store.status == "provisioning" and "retrying - helm install" in store.status_reason
    async with db.session() as s:
        store = await s.get(Store, sid)
        store.deadline_at = utcnow() - timedelta(seconds=1)
        await s.commit()
    await p.reconcile_store(sid)
    async with db.session() as s:
        store = await s.get(Store, sid)
    assert store.status == "failed"


async def _noop():
    return None


async def test_delete_removes_registration_release_and_namespace(env):
    _, db, kube, helm, p = env
    sid = await add_store(db)
    await p.reconcile_store(sid)
    async with db.session() as s:
        (await s.get(Store, sid)).status = "deleting"
        await s.commit()
    await p.reconcile_store(sid)
    async with db.session() as s:
        assert await s.get(Store, sid) is None
        assert await s.scalar(select(UpstreamServer).where(UpstreamServer.slug == "shop-berlin")) is None
    assert "store-berlin" not in kube.namespaces and not kube.secrets
    assert any(c[0] == "uninstall" for c in helm.calls)


async def test_hosted_server_gets_isolated_namespace(env):
    _, db, kube, _, p = env
    async with db.session() as s:
        server = UpstreamServer(slug="calc", name="Calc", url="http://calc.mcp-calc.svc.cluster.local:8000/mcp", hosted_image="utils-mcp:dev",
                                hosted_port=8000, hosted_path="/mcp", hosted_env_enc=p.box.seal(json.dumps({"API_TOKEN": "x"})), hosted_state="deploying")
        s.add(server)
        await s.commit()
        sid = server.id
    await p.reconcile_hosted(sid)
    kinds = {o["kind"]: o for o in kube.applied}
    assert set(kinds) == {"ResourceQuota", "LimitRange", "NetworkPolicy", "Deployment", "Service"}
    pod = kinds["Deployment"]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] and not pod["automountServiceAccountToken"]
    assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"]
    netpol = kinds["NetworkPolicy"]["spec"]
    assert netpol["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "gateway"}
    assert len(netpol["egress"]) == 1  # DNS only unless egress is allowed
    assert kube.secrets[("mcp-calc", "server-env")] == {"API_TOKEN": "x"}
    async with db.session() as s:
        assert (await s.get(UpstreamServer, sid)).hosted_state == "ready"
    async with db.session() as s:
        (await s.get(UpstreamServer, sid)).hosted_state = "deleting"
        await s.commit()
    await p.reconcile_hosted(sid)
    async with db.session() as s:
        assert await s.get(UpstreamServer, sid) is None
    assert "mcp-calc" not in kube.namespaces
