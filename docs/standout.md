# Bonus and stand-out items

| Item | Status | Where |
|---|---|---|
| **2. Tool permissions** | Done. Per-tool *admins only* flag, editable in the dashboard. Restricted tools are hidden from `tools/list` (MCP clients, chat, dashboard) and rejected if called by name, audited as `denied`. Seeded on `create_product`, `update_order_status`, `create_shipment`. | `gateway/mcp_endpoint.py` (`allowed`, `visible_tools`), Servers page |
| **4. Handling many tools** | Done for the chat agent. Above `CHAT_MAX_TOOLS` (40) the model gets `search_tools` + `call_tool` meta-tools instead of every schema. The external MCP endpoint still lists everything. | `gateway/chat.py` |
| **6. Security hardening** | SSRF guard on admin URLs (save time and every connection: loopback, link-local, metadata, private and cluster ranges unless allow-listed, credentials in URL). Upstream credentials encrypted at rest (Fernet). Default-deny NetworkPolicies in every namespace. All containers non-root with dropped capabilities and seccomp, including WordPress (uid 33 on :8080). No service account tokens mounted. | `gateway/ssrf.py`, `gateway/security.py`, `charts/*/templates/networkpolicy.yaml` |
| **7. Scaling** | Two gateway replicas with stateless Streamable HTTP, health/tool cache in Postgres, and a per-user rate limit shared across replicas (sliding 60 s window from the audit log). | `charts/platform/values.yaml` (`gateway.replicas`), `gateway/audit.py` |
| **8. Observability** | Prometheus `/metrics` (cluster-internal): tool calls by server, source and outcome, latency histogram per server, upstream up/down, probe latency, auth failures, chat outcomes. Dashboard shows per-server 24 h calls, error rate and average latency, and a plain-language reason for every unreachable upstream and failed call. | `gateway/metrics.py`, Audit page |
| **9. Upgrades and rollback** | Documented: Helm history and rollback, rolling gateway updates with `maxUnavailable: 0`, an idempotent store setup Job on every upgrade, and the database-migration caveat with a backup step. | README, *Upgrades and rollback* |
| **1. Production-like VPS** | Charts and values ready (`values-prod.yaml`, cert-manager ClusterIssuer, registry images). Not demonstrated on a live VPS. | README, `deploy/cluster-issuer.yaml` |
| Bonus: multi-store provisioning | Not built (core first). | — |
| 3. OAuth on the gateway, 5. Hosted MCP servers | Not attempted. | — |
