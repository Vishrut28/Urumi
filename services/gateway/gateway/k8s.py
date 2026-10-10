"""Small async helpers over the Kubernetes API, used only by the provisioner.

The provisioner's service account can create and delete namespaces whose name
starts with `store-` or `mcp-` (enforced by a ValidatingAdmissionPolicy), and
binds itself to the `store-namespace-admin` ClusterRole inside each one. It has
no rights anywhere else, so every other call here operates inside a namespace it
created.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException

log = logging.getLogger("gateway.k8s")

NAMESPACE_ADMIN_ROLE = "store-namespace-admin"


class Kube:
    def __init__(self) -> None:
        self.api: client.ApiClient | None = None
        self.service_account = os.environ.get("PROVISIONER_SERVICE_ACCOUNT", "store-provisioner")
        self.own_namespace = os.environ.get("PROVISIONER_GATEWAY_NAMESPACE", "platform")

    async def connect(self) -> None:
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            config.load_incluster_config()
        else:
            await config.load_kube_config()
        self.api = client.ApiClient()

    async def close(self) -> None:
        if self.api:
            await self.api.close()

    @property
    def core(self) -> client.CoreV1Api:
        return client.CoreV1Api(self.api)

    @property
    def apps(self) -> client.AppsV1Api:
        return client.AppsV1Api(self.api)

    @property
    def rbac(self) -> client.RbacAuthorizationV1Api:
        return client.RbacAuthorizationV1Api(self.api)

    @property
    def net(self) -> client.NetworkingV1Api:
        return client.NetworkingV1Api(self.api)

    # --- namespaces ---------------------------------------------------------

    async def ensure_namespace(self, name: str, labels: dict[str, str]) -> None:
        """Create the namespace (idempotent) and give our service account admin rights inside it."""
        try:
            await self.core.create_namespace({"metadata": {"name": name, "labels": labels}})
            log.info("created namespace %s", name)
        except ApiException as exc:
            if exc.status != 409:
                raise
            ns = await self.core.read_namespace(name)
            if (ns.status.phase or "") == "Terminating":
                raise RuntimeError(f"namespace {name} is still terminating from an earlier delete") from None
        binding = {
            "metadata": {"name": "store-provisioner", "namespace": name},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": NAMESPACE_ADMIN_ROLE},
            "subjects": [{"kind": "ServiceAccount", "name": self.service_account, "namespace": self.own_namespace}],
        }
        try:
            await self.rbac.create_namespaced_role_binding(name, binding)
        except ApiException as exc:
            if exc.status != 409:
                raise
        # RBAC caches propagate quickly but not instantly.
        for _ in range(20):
            try:
                await self.core.list_namespaced_secret(name, limit=1)
                return
            except ApiException as exc:
                if exc.status != 403:
                    raise
                await asyncio.sleep(0.5)
        raise RuntimeError(f"no permissions in namespace {name} after binding")

    async def namespace_exists(self, name: str) -> bool:
        try:
            await self.core.read_namespace(name)
            return True
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise

    async def delete_namespace(self, name: str, wait_seconds: float = 300) -> None:
        try:
            await self.core.delete_namespace(name)
        except ApiException as exc:
            if exc.status != 404:
                raise
            return
        for _ in range(int(wait_seconds / 2)):
            if not await self.namespace_exists(name):
                log.info("namespace %s deleted", name)
                return
            await asyncio.sleep(2)
        raise RuntimeError(f"namespace {name} still terminating after {wait_seconds:.0f}s")

    # --- secrets --------------------------------------------------------------

    async def ensure_secret(self, namespace: str, name: str, data: dict[str, str], labels: dict[str, str] | None = None) -> dict[str, str]:
        """Create the Secret if missing and return its current data (existing values win: safe to retry)."""
        try:
            secret = await self.core.read_namespaced_secret(name, namespace)
            return {k: base64.b64decode(v).decode() for k, v in (secret.data or {}).items()}
        except ApiException as exc:
            if exc.status != 404:
                raise
        await self.core.create_namespaced_secret(namespace, {"metadata": {"name": name, "labels": labels or {}}, "type": "Opaque", "stringData": data})
        return data

    async def replace_secret(self, namespace: str, name: str, data: dict[str, str], labels: dict[str, str] | None = None) -> None:
        body = {"metadata": {"name": name, "labels": labels or {}}, "type": "Opaque", "stringData": data}
        try:
            await self.core.replace_namespaced_secret(name, namespace, body)
        except ApiException as exc:
            if exc.status != 404:
                raise
            await self.core.create_namespaced_secret(namespace, body)

    # --- generic apply (hosted MCP servers) ---------------------------------------

    async def apply(self, obj: dict[str, Any]) -> None:
        """Create or replace one of the few object kinds a hosted server needs."""
        kind, ns, name = obj["kind"], obj["metadata"]["namespace"], obj["metadata"]["name"]
        ops = {
            "Deployment": (self.apps.create_namespaced_deployment, self.apps.replace_namespaced_deployment),
            "Service": (self.core.create_namespaced_service, None),
            "NetworkPolicy": (self.net.create_namespaced_network_policy, self.net.replace_namespaced_network_policy),
            "ResourceQuota": (self.core.create_namespaced_resource_quota, self.core.replace_namespaced_resource_quota),
            "LimitRange": (self.core.create_namespaced_limit_range, self.core.replace_namespaced_limit_range),
        }
        create, replace = ops[kind]
        try:
            await create(ns, obj)
        except ApiException as exc:
            if exc.status != 409:
                raise
            if replace is not None:
                await replace(name, ns, obj)

    async def deployment_ready(self, namespace: str, name: str) -> tuple[bool, str]:
        try:
            dep = await self.apps.read_namespaced_deployment(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return False, "deployment not found"
            raise
        if (dep.status.available_replicas or 0) >= 1:
            return True, "available"
        pods = await self.core.list_namespaced_pod(namespace, label_selector=f"app.kubernetes.io/name={name}")
        for pod in pods.items:
            for cs in pod.status.container_statuses or []:
                waiting = cs.state.waiting if cs.state else None
                if waiting and waiting.reason in ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "CreateContainerConfigError", "InvalidImageName"):
                    return False, f"{waiting.reason}: {(waiting.message or '')[:200]}"
        return False, "starting"
