"""Store and hosted-server provisioner: a reconcile loop over intent recorded in Postgres.

Run as its own Deployment (`python -m gateway.provisioner`), one replica, with
the only service account in the platform that may call the Kubernetes API.

Stores (status provisioning -> ready | failed, deleting -> gone):
  namespace store-<slug> (+ RoleBinding) -> Secret with random credentials ->
  `helm upgrade --install` of the same store chart as the main store (with
  quota, limits and NetworkPolicies) -> register its woo-mcp in the gateway as
  `shop-<slug>` -> wait until the gateway can list its tools -> ready.
  Every step is idempotent, so a restart mid-way simply runs the store again;
  past its deadline a store fails with the step and last error as the reason.
Hosted servers (hosted_state deploying -> ready | failed, deleting -> gone):
  namespace mcp-<slug> with Secret, Deployment, Service, deny-by-default
  NetworkPolicy, ResourceQuota and LimitRange, then registered by URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, text

from gateway.config import Settings, get_settings
from gateway.db import Database, Store, UpstreamServer, User, utcnow
from gateway.k8s import Kube
from gateway.security import CredentialBox
from gateway.stores import store_server_slug
from gateway.upstreams import HealthMonitor

log = logging.getLogger("gateway.provisioner")

STORE_ADMIN_ONLY = ["create_product", "update_order_status"]  # the owner and admins keep them


class StepError(Exception):
    pass


class Provisioner:
    def __init__(self, settings: Settings, db: Database, box: CredentialBox, kube: Kube):
        self.s = settings
        self.db = db
        self.box = box
        self.kube = kube
        allowed = [p.strip() for p in settings.ssrf_allowed_internal_hosts.split(",")]
        self.monitor = HealthMonitor(db, box, settings.health_interval_seconds, settings.health_timeout_seconds, allowed, settings.ssrf_allow_all)
        self.tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._loop: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._loop = asyncio.create_task(self._run(), name="provisioner")

    async def stop(self) -> None:
        for t in [self._loop, *self.tasks.values()]:
            if t:
                t.cancel()
        await asyncio.gather(*(t for t in [self._loop, *self.tasks.values()] if t), return_exceptions=True)

    async def _run(self) -> None:
        log.info("provisioner started")
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("reconcile tick failed")
            await asyncio.sleep(3)

    async def tick(self) -> None:
        async with self.db.session() as s:
            store_ids = (await s.scalars(select(Store.id).where(Store.status.in_(("provisioning", "deleting"))))).all()
            server_ids = (await s.scalars(select(UpstreamServer.id).where(UpstreamServer.hosted_state.in_(("deploying", "deleting"))))).all()
        for kind, ids, fn in (("store", store_ids, self.reconcile_store), ("hosted", server_ids, self.reconcile_hosted)):
            for i in ids:
                task = self.tasks.get((kind, i))
                if task is None or task.done():
                    self.tasks[(kind, i)] = asyncio.create_task(fn(i), name=f"{kind}-{i}")

    # --- stores -----------------------------------------------------------------

    async def _update_store(self, store_id: int, **fields: Any) -> None:
        async with self.db.session() as s:
            store = await s.get(Store, store_id)
            if store is not None:
                for k, v in fields.items():
                    setattr(store, k, v)
                await s.commit()

    async def reconcile_store(self, store_id: int) -> None:
        async with self.db.session() as s:
            store = await s.get(Store, store_id)
            owner = await s.get(User, store.owner_id) if store else None
        if store is None:
            return
        if store.status == "deleting":
            await self._delete_store(store)
            return
        step = "starting"
        try:
            await self._update_store(store_id, attempts=(store.attempts or 0) + 1)
            steps = (
                ("namespace", lambda: self._store_namespace(store)),
                ("secrets", lambda: self._store_secrets(store)),
                ("helm install (WordPress, WooCommerce, woo-mcp)", lambda: self._store_helm(store, owner)),
                ("register MCP server", lambda: self._store_register(store)),
                ("connect MCP server", lambda: self._store_connect(store)),
            )
            for step, run in steps:
                if utcnow() > store.deadline_at:
                    raise StepError(f"provisioning timed out after {self.s.provisioner_timeout_seconds}s")
                await self._update_store(store_id, step=step)
                await run()
            await self._update_store(store_id, status="ready", status_reason=None, step="done")
            log.info("store %s ready", store.slug)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"{step}: {exc}"[:1000]
            if utcnow() > store.deadline_at or isinstance(exc, StepError):
                log.warning("store %s failed: %s", store.slug, reason)
                await self._update_store(store_id, status="failed", status_reason=reason)
            else:  # transient: keep the reason visible and let the next tick retry
                log.warning("store %s step failed, will retry: %s", store.slug, reason)
                await self._update_store(store_id, status_reason=f"retrying - {reason}")
                await asyncio.sleep(10)

    async def _store_namespace(self, store: Store) -> None:
        await self.kube.ensure_namespace(store.namespace, {
            "woo-mcp-gateway/store": "true",
            "woo-mcp-gateway/store-id": str(store.id),
            "app.kubernetes.io/managed-by": "store-provisioner",
        })

    async def _store_secrets(self, store: Store) -> None:
        await self.kube.ensure_secret(store.namespace, "store-secrets", {
            "db-root-password": secrets.token_hex(24),
            "db-password": secrets.token_hex(24),
            "wp-admin-password": secrets.token_hex(12),
            "wc-consumer-key": "ck_" + secrets.token_hex(20),
            "wc-consumer-secret": "cs_" + secrets.token_hex(20),
            "mcp-token": secrets.token_hex(32),
        })

    async def _store_helm(self, store: Store, owner: User | None) -> None:
        release, ns = f"store-{store.slug}", store.namespace
        status = await _helm("status", release, "-n", ns, "-o", "json", check=False)
        if status.ok:
            state = json.loads(status.out).get("info", {}).get("status", "")
            if state.startswith("pending-"):
                # The provisioner died mid-install. The store is new, so start over cleanly.
                log.warning("release %s stuck in %s; uninstalling before retry", release, state)
                await _helm("uninstall", release, "-n", ns, "--wait", "--timeout", "180s", check=False)
        values = {
            "store": {"host": store.url.split("://", 1)[1], "scheme": self.s.provisioner_store_scheme, "title": store.name,
                      "adminEmail": owner.email if owner else "admin@example.com"},
            "existingSecret": "store-secrets",
            "images": {"wooMcp": {"repository": self.s.provisioner_store_image, "tag": self.s.provisioner_store_image_tag, "pullPolicy": "IfNotPresent"}},
            "ingress": {"className": self.s.provisioner_ingress_class},
            "guardrails": {"enabled": True},
            "networkPolicy": {"gatewayNamespace": self.s.provisioner_gateway_namespace},
        }
        remaining = max(60, int((store.deadline_at - utcnow()).total_seconds()))
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            json.dump(values, f)  # JSON is valid YAML
            values_file = f.name
        try:
            args = ["upgrade", "--install", release, self.s.provisioner_chart_path, "-n", ns, "-f", values_file]
            if self.s.provisioner_store_values_file and os.path.exists(self.s.provisioner_store_values_file):
                args += ["-f", self.s.provisioner_store_values_file]
            await _helm(*args, "--wait", "--timeout", f"{remaining}s")
        finally:
            os.unlink(values_file)

    async def _store_register(self, store: Store) -> None:
        token = (await self.kube.ensure_secret(store.namespace, "store-secrets", {}))["mcp-token"]
        slug = store_server_slug(store.slug)
        async with self.db.session() as s:
            server = await s.scalar(select(UpstreamServer).where(UpstreamServer.slug == slug))
            if server is not None and server.managed_by != f"store:{store.id}":
                raise StepError(f"gateway already has a server with prefix {slug}")
            if server is None:
                server = UpstreamServer(slug=slug, managed_by=f"store:{store.id}")
                s.add(server)
            server.name = f"Store: {store.name}"
            server.url = f"http://woo-mcp.{store.namespace}.svc.cluster.local:8000/mcp"
            server.auth_type, server.credential_enc = "bearer", self.box.seal(token)
            server.enabled, server.owner_id = True, store.owner_id
            server.admin_only_tools = STORE_ADMIN_ONLY
            server.timeout_seconds = self.s.default_call_timeout_seconds
            await s.commit()

    async def _store_connect(self, store: Store) -> None:
        slug = store_server_slug(store.slug)
        reason = "not checked"
        while utcnow() < store.deadline_at:
            async with self.db.session() as s:
                server = await s.scalar(select(UpstreamServer).where(UpstreamServer.slug == slug))
            if server is None:
                raise RuntimeError("MCP server registration disappeared")
            await self.monitor.probe(server.id)
            async with self.db.session() as s:
                server = await s.get(UpstreamServer, server.id)
            if server and server.status == "connected":
                return
            reason = (server.status_reason if server else None) or "unknown"
            await asyncio.sleep(5)
        raise StepError(f"gateway could not reach the store's MCP server: {reason}")

    async def _delete_store(self, store: Store) -> None:
        try:
            await self._update_store(store.id, step="unregister MCP server")
            async with self.db.session() as s:
                await s.execute(delete(UpstreamServer).where(UpstreamServer.managed_by == f"store:{store.id}"))
                await s.commit()
            await self._update_store(store.id, step="helm uninstall")
            res = await _helm("uninstall", f"store-{store.slug}", "-n", store.namespace, "--wait", "--timeout", "300s", check=False)
            if not res.ok and "not found" not in res.err.lower():
                raise RuntimeError(res.err.strip().splitlines()[-1] if res.err.strip() else "helm uninstall failed")
            await self._update_store(store.id, step="delete namespace (volumes, secrets)")
            await self.kube.delete_namespace(store.namespace)
            async with self.db.session() as s:
                await s.execute(delete(Store).where(Store.id == store.id))
                await s.commit()
            log.info("store %s deleted", store.slug)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("deleting store %s failed, will retry: %s", store.slug, exc)
            await self._update_store(store.id, status_reason=f"retrying delete - {exc}"[:1000])
            await asyncio.sleep(10)

    # --- hosted MCP servers ---------------------------------------------------------

    async def _update_server(self, server_id: int, **fields: Any) -> None:
        async with self.db.session() as s:
            server = await s.get(UpstreamServer, server_id)
            if server is not None:
                for k, v in fields.items():
                    setattr(server, k, v)
                await s.commit()

    async def reconcile_hosted(self, server_id: int) -> None:
        async with self.db.session() as s:
            server = await s.get(UpstreamServer, server_id)
        if server is None or not server.hosted_image:
            return
        ns = f"mcp-{server.slug}"
        if server.hosted_state == "deleting":
            try:
                await self.kube.delete_namespace(ns)
                async with self.db.session() as s:
                    await s.execute(delete(UpstreamServer).where(UpstreamServer.id == server_id))
                    await s.commit()
                log.info("hosted server %s removed", server.slug)
            except Exception as exc:
                await self._update_server(server_id, hosted_reason=f"retrying delete - {exc}"[:1000])
                await asyncio.sleep(10)
            return
        deadline = utcnow() + timedelta(seconds=self.s.provisioner_hosted_timeout_seconds)
        try:
            await self.kube.ensure_namespace(ns, {"woo-mcp-gateway/hosted": "true", "app.kubernetes.io/managed-by": "store-provisioner"})
            env = json.loads(self.box.open(server.hosted_env_enc)) if server.hosted_env_enc else {}
            await self.kube.replace_secret(ns, "server-env", {str(k): str(v) for k, v in env.items()})
            for obj in hosted_objects(server, ns, self.s.provisioner_gateway_namespace):
                await self.kube.apply(obj)
            reason = "starting"
            while utcnow() < deadline:
                ready, reason = await self.kube.deployment_ready(ns, server.slug)
                if ready:
                    break
                if reason.split(":")[0] in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CreateContainerConfigError"):
                    raise StepError(reason)
                await asyncio.sleep(3)
            else:
                raise StepError(f"not ready after {self.s.provisioner_hosted_timeout_seconds}s ({reason})")
            await self._update_server(server_id, hosted_state="ready", hosted_reason=None, status="unknown")
            await self.monitor.probe(server_id)
            log.info("hosted server %s ready", server.slug)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("hosted server %s failed: %s", server.slug, exc)
            await self._update_server(server_id, hosted_state="failed", hosted_reason=str(exc)[:1000])


def hosted_objects(server: UpstreamServer, ns: str, gateway_ns: str) -> list[dict[str, Any]]:
    name, port = server.slug, server.hosted_port or 8000
    labels = {"app.kubernetes.io/name": name, "mcp-gateway/hosted-server": "true"}
    egress: list[dict[str, Any]] = [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                                     "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]
    if server.hosted_allow_egress:
        egress.append({"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"]}}],
                       "ports": [{"protocol": "TCP", "port": 443}]})
    return [
        {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "hosted-quota", "namespace": ns},
         "spec": {"hard": {"requests.cpu": "500m", "requests.memory": "512Mi", "limits.cpu": "1", "limits.memory": "1Gi", "pods": "3"}}},
        {"apiVersion": "v1", "kind": "LimitRange", "metadata": {"name": "hosted-limits", "namespace": ns},
         "spec": {"limits": [{"type": "Container", "default": {"cpu": "500m", "memory": "256Mi"}, "defaultRequest": {"cpu": "25m", "memory": "64Mi"}}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "hosted-server", "namespace": ns},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                  "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": gateway_ns}},
                                         "podSelector": {"matchLabels": {"app.kubernetes.io/name": "gateway"}}}],
                               "ports": [{"protocol": "TCP", "port": port}]}],
                  "egress": egress}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns, "labels": labels},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "automountServiceAccountToken": False, "enableServiceLinks": False,
                      "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                      "containers": [{
                          "name": "mcp", "image": server.hosted_image, "imagePullPolicy": "IfNotPresent",
                          "ports": [{"name": "mcp", "containerPort": port}],
                          "envFrom": [{"secretRef": {"name": "server-env"}}],
                          "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                          "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
                          "readinessProbe": {"tcpSocket": {"port": port}, "periodSeconds": 5},
                          "livenessProbe": {"tcpSocket": {"port": port}, "initialDelaySeconds": 10, "periodSeconds": 20},
                          "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                      }],
                      "volumes": [{"name": "tmp", "emptyDir": {}}],
                  }}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns, "labels": labels},
         "spec": {"selector": {"app.kubernetes.io/name": name}, "ports": [{"name": "mcp", "port": port, "targetPort": port}]}},
    ]


class _Result:
    def __init__(self, code: int, out: str, err: str):
        self.ok, self.out, self.err = code == 0, out, err


async def _helm(*args: str, check: bool = True) -> _Result:
    proc = await asyncio.create_subprocess_exec("helm", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    res = _Result(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
    if check and not res.ok:
        last = [line for line in res.err.strip().splitlines() if line.strip()]
        raise RuntimeError(f"helm {args[0]} failed: {last[-1][:500] if last else 'no output'}")
    return res


def app() -> FastAPI:  # uvicorn --factory gateway.provisioner:app
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = get_settings()
    settings.require_secrets()
    db = Database(settings.database_url)
    kube = Kube()
    provisioner = Provisioner(settings, db, CredentialBox(settings.credentials_key), kube)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await db.create_all()
        await kube.connect()
        provisioner.start()
        try:
            yield
        finally:
            await provisioner.stop()
            await kube.close()
            await db.dispose()

    api = FastAPI(title="provisioner", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/healthz")
    async def healthz() -> JSONResponse:
        alive = provisioner._loop is not None and not provisioner._loop.done()
        return JSONResponse({"ok": alive}, status_code=200 if alive else 500)

    @api.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            async with db.session() as s:
                await s.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse({"ok": False, "reason": type(exc).__name__}, status_code=503)
        return JSONResponse({"ok": True})

    return api
