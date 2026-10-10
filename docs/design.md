# System design and tradeoffs

## Components and responsibilities

| Component | Responsibility | State |
|---|---|---|
| WordPress + WooCommerce (`store-main` ns) | The store and storefront | MariaDB PVC, `wp-content` PVC |
| `store-setup` Job | Idempotent WP-CLI bootstrap: WooCommerce, COD, pages, REST API key, sample products. Re-runs on every `helm upgrade` | none |
| `woo-mcp` | Store products and orders as MCP tools over Streamable HTTP | none |
| `gateway` (×2) | Dashboard REST API, aggregated MCP endpoint, upstream health, chat agent | Postgres |
| `dashboard` | React SPA served by unprivileged nginx | none |
| `shipping-mcp`, `utils-mcp` | The other two upstreams | in-memory only |
| `provisioner` | Creates and deletes stores and hosted MCP servers (only Kubernetes API client) | Postgres |
| Postgres | Users, invites, API key hashes, upstream servers (encrypted credentials, cached tools, health), audit log | PVC |

Two Helm releases (`charts/store` into `store-main`, `charts/platform` into
`platform`) rather than one umbrella chart. A store is a self-contained unit
(namespace, database, volumes, Secrets, Ingress host, MCP server), so the same
chart can be installed again for further stores. The gateway lives apart from
any one store.

## How woo-mcp talks to the store

It uses the **WooCommerce REST API (`wc/v3`)** over in-cluster HTTP
(`http://wordpress:8080`), authenticated with a dedicated read/write REST key.

- **Why REST and not WP-CLI or direct SQL.** REST is the supported, versioned
  interface. It runs WooCommerce's own validation, hooks, stock handling and order
  notes, and it needs no access to the WordPress filesystem or database. So
  woo-mcp gets no volume mounts and no database credentials. WP-CLI would need a
  shared volume (ReadWriteOnce, so the same node) and a shell per call. SQL would
  bypass WooCommerce's business logic, and HPOS order tables make it brittle.
- **Why OAuth 1.0a signatures.** WooCommerce accepts Basic auth only over TLS
  (`is_ssl()`), and in-cluster traffic is plain HTTP. woo-mcp signs each request
  with one-legged OAuth 1.0a exactly as WooCommerce verifies it. The base URL is
  the store's home URL (read from `/wp-json/`) with the scheme forced to `http`,
  not the in-cluster host name. `tests/test_woo_mcp.py` checks the signature
  against a line-by-line port of WooCommerce's PHP verifier. Faking
  `X-Forwarded-Proto: https` to unlock Basic auth would also work, but it relies
  on a proxy-header trust quirk rather than the documented scheme.
- **Key provisioning.** The REST key comes from the store's Secret. The setup Job
  writes it into `wp_woocommerce_api_keys` the same way WooCommerce does (consumer
  key stored as `wc_api_hash`). Rotating the Secret and running `helm upgrade`
  replaces the old key.

## Tool aggregation, routing and naming

- **Discovery.** Each gateway replica runs a health loop (every 10 s). It opens a
  Streamable HTTP session to every enabled upstream with a 5 s deadline, calls
  `tools/list` (following pagination), and writes the result, status and reason
  into the upstream's database row. Every replica therefore serves the same view,
  and a dead upstream never makes `tools/list` slow.
- **`tools/list` through the gateway** reads that cache. It returns the tools of
  servers that are enabled and connected, minus tools the caller's role may not use.
- **Naming scheme: always `<slug>__<tool>`.** Every tool is prefixed, not just the
  colliding ones. `woocommerce__get_order` and `shipping__get_order` live side by
  side. Names stay stable when a server with a clashing name is added later
  (conditional prefixing would silently rename tools in clients that already use
  them). The prefix also tells the model which system a tool acts on. Slugs are
  `[a-z0-9-]`, so they can't contain `__`, and routing is a split on the first
  `__`. Characters outside `[A-Za-z0-9_-]` become `_`, names are cut to 64
  characters (OpenAI- and Gemini-style function names), and any clash that
  creates inside one server gets a numeric suffix. Descriptions are prefixed with
  the server's display name and capped at 1 KB.
- **`tools/call`.** The name resolves to (server, upstream tool name). Then:
  permission check, rate limit (shared across replicas, counted from the audit
  log), SSRF re-check of the upstream URL, then a fresh upstream session with the
  server-side credential and a hard deadline (per-server timeout, default 20 s).
  The result goes back unchanged, and an audit row is written.

```
client ─POST /mcp tools/call woocommerce__list_orders (Bearer mgw_…)─► Traefik ─► gateway pod
  McpAuth: HMAC(key) lookup → user, role          (revoked → 401, audited)
  resolve slug → upstream row, entry → upstream name "list_orders"
  allowed(role, admin_only_tools)? rate limit? SSRF guard?
  open session to http://woo-mcp.store-main.svc:8000/mcp with "Authorization: Bearer <server credential>"
     woo-mcp → OAuth1-signed GET http://wordpress:8080/wp-json/wc/v3/orders?after=…  → MariaDB
  ◄─ CallToolResult (content + structuredContent)
  audit row (user, source, key prefix, server, tool, ms, outcome)
◄─ same CallToolResult
```

## Failure handling

| Situation | What happens |
|---|---|
| Upstream slow | The call fails at its deadline: `Upstream 'Shipping' failed: no response within 20s. Other servers are unaffected.` (`isError: true`), audited as `timeout`. Nothing is shared between upstreams, so other calls proceed. |
| Upstream pod killed | Connection refused, so the call returns a clear error, the server is marked **Unreachable** immediately with reason "connection refused or no route to host", and its tools leave the list. The health loop marks it Connected again when it comes back. Other servers are untouched. |
| Wrong credential | The reason reads "upstream answered HTTP 401 - check the credential" (the gateway records the transport's last HTTP status, which the MCP client library does not surface). |
| DNS failure / blocked URL | "host name does not resolve" / "URL refused by SSRF guard: … resolves to a cloud metadata endpoint". |
| Tool error inside upstream | Passed through unchanged (`isError: true`), audited as `error` with the message. |
| Gateway replica dies | Stateless Streamable HTTP (no session affinity needed), so the next request goes to the other replica. |
| Postgres down | `/readyz` fails, so replicas leave the Service, and liveness stays green (no restart storm). |
| LLM rate limit (free tier) | The chat shows "free-tier rate limit was reached, wait a minute". |

## Chat agent

`POST /api/chat` runs a bounded tool loop (8 steps) against an OpenAI-compatible
chat-completions API. The default is Gemini 3.5 Flash (`gemini-3.5-flash`, free tier, `reasoning_effort: low`) through Google's
OpenAI-compatible endpoint; the model, base URL and key are values plus a Secret.
The agent is an **MCP client of the gateway's own `/mcp`**. It authenticates with
a five-minute HMAC token bound to the logged-in user (`c.<payload>.<sig>`). So its
tool list, permissions, rate limit and audit rows are exactly that user's, with
`source=chat`. It never holds an API key or an upstream credential. JSON Schemas
are reduced to the subset Gemini accepts (`$ref` inlined, `anyOf[…, null]`
collapsed). Assistant messages are echoed back verbatim so Gemini's thought
signatures survive multi-step tool calls. With more than 40 tools, the agent gets
two meta-tools instead (`search_tools`, `call_tool`) so the prompt stays small.

## Store provisioning (bonus) and hosted MCP servers

The gateway only records intent (a `stores` row, or an upstream row with
`hosted_image`). A separate **provisioner** Deployment reconciles it every 3 s,
one task per store or server. It's the only workload with a Kubernetes service
account token.

```
POST /api/stores {slug: paris}         gateway: row status=provisioning, deadline = now + 15 min
provisioner tick ──► namespace store-paris (+ labels, + RoleBinding to store-namespace-admin)
                 ──► Secret store-secrets (random; existing values win, so retries keep them)
                 ──► helm upgrade --install store-paris /charts/store --wait (quota, limits, NetworkPolicies, TLS in prod)
                 ──► upstream row shop-paris (bearer = the store's mcp-token, owner = requester)
                 ──► probe until the gateway lists its tools ──► status=ready
DELETE                ──► unregister (tools vanish now) ──► helm uninstall ──► delete namespace ──► delete row
```

- **Why a reconciler and not the API request itself:** creating a store takes
  minutes and must survive restarts. Every step is idempotent. A `helm` release
  left `pending-install` by a killed provisioner is uninstalled and installed
  again (the store is new, so there's no data to lose). After the deadline the
  store becomes `failed` with the step and the last error. `POST` with the same
  slug returns the same store, and *Retry* re-queues a failed one.
- **Why helm, with the same chart as the main store:** one definition of a
  store, and an upgrade path for provisioned stores that is the same
  `helm upgrade` as everything else.
- **Tool names** are `shop-<slug>__<tool>`, so the store is visible in every
  tool name and audit row.
- **Hosted servers** use plain Kubernetes objects rather than a chart: Secret
  (env), Deployment (non-root, read-only root, limits, TCP probes), Service,
  NetworkPolicy (gateway in, DNS out, optional public HTTPS), ResourceQuota and
  LimitRange in `mcp-<slug>`. Image pull errors are surfaced as the reason.

## OAuth on the gateway

`gateway/oauth.py` is a small OAuth 2.1 authorization server co-located with the
resource server. A 401 from `/mcp` carries
`WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/mcp"`.
Clients discover the issuer and register: by Client ID Metadata Document (fetched
through the SSRF guard) or dynamic registration. They then send the browser to
`/oauth/authorize` with PKCE S256 and `resource`. The dashboard shows a consent
page as the signed-in user, and the code is exchanged at `/oauth/token`.

Access tokens (1 h) and refresh tokens (30 d) are opaque. They're stored as HMACs
like API keys and bound to one user and to this gateway's MCP URL (RFC 8707). The
MCP endpoint rejects tokens issued for another resource. Refresh tokens rotate,
and replaying an old one revokes the whole grant. The authorization response
carries `iss` (RFC 9207). API keys remain for clients without OAuth support.

## Many tools

With ten upstreams, a client can see hundreds of tools. `/mcp?tools=discover`
replaces the list with `gateway__list_servers`, `gateway__search_tools` (keyword
ranking over names and descriptions, optionally per server) and
`gateway__call_tool`. Search results only include tools the caller may use, and
calls go through the same `invoke()` path (permissions, rate limit, audit). The
chat agent uses the same pattern automatically above 40 tools.

## Replicas and scaling

The MCP endpoint runs `stateless_http=True` with JSON responses: no server-side
MCP sessions, no SSE streams to pin, so any replica can serve any request. All
shared state (health, tool cache, rate-limit counts, audit) is in Postgres. The
cost is one upstream handshake per call; the benefit is that there's no
connection pool to keep consistent across replicas, and one bad upstream can't
wedge a shared connection.

## What changes for production

- **DNS and ingress:** `publicHost`/`store.host` become real names (or `sslip.io`).
  Traefik stays (k3s default). TLS comes from cert-manager via
  `ingress.tls.clusterIssuer`, and `scheme: https` makes the session cookie
  `Secure` and the store URLs https.
- **Storage class:** `local-path` works on a single-node k3s. On multi-node,
  use Longhorn or a cloud CSI class (`persistence.storageClass`,
  `postgres.storageClass`). WordPress uses `Recreate` because its volume is RWO.
- **Secrets:** the charts only reference Secret names. `scripts/secrets.sh`
  generates them; in production, create the same names from a secret manager
  (External Secrets Operator, Sealed Secrets, SOPS). Nothing secret is in git or
  in values files.
- **Images:** pushed to a registry (`images.registry`, `images.tag`) instead of `k3d image import`.

## Tradeoffs and limits

- Health is polled (10 s), not pushed. A dead upstream is noticed at the next
  call or sweep. Stateless MCP can't push `tools/list_changed` to clients, so clients
  see changes on their next `tools/list`.
- A per-call upstream session costs a handshake (a few ms in-cluster). That
  was chosen over pooled sessions for isolation and replica-safety.
- The rate limit counts audit rows, so it's a sliding 60 s window, consistent
  across replicas, and needs no extra infrastructure. At high volume a Redis
  token bucket would be cheaper.
- `shipping-mcp` keeps shipments in memory (a demo carrier). It forgets them on restart.
- Invites produce a link rather than an email (there's no mail server).
