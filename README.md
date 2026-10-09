# WooCommerce hosting with a team MCP gateway

A WooCommerce store on Kubernetes, an MCP server for it written from scratch, and
a team gateway. An admin connects MCP servers once in a dashboard. Every team
member then uses all of them through one MCP endpoint with their own API key,
or through a chat agent in the dashboard.

```
                    Ingress (Traefik)
     shop.localtest.me │         │ app.localtest.me  (/ , /api , /mcp)
                       ▼         ▼
 ┌─ ns store-main ─────────┐   ┌─ ns platform ──────────────────────────────────┐
 │ WordPress+WooCommerce   │   │ dashboard (React, nginx)                       │
 │   │          ▲          │   │ gateway ×2 (FastAPI)                            │
 │ MariaDB      │ REST     │   │   /api  dashboard API, chat agent               │
 │ (PVC)   woo-mcp ◄───────┼───┼── /mcp  one MCP endpoint, API-key auth         │
 │ (PVC)   ClusterIP only  │   │   ├─► shipping-mcp (header credential)         │
 └─────────────────────────┘   │   ├─► utils-mcp    (no credential)             │
                               │   └─► Postgres (users, keys, servers, audit)   │
                               └────────────────────────────────────────────────┘
   Claude Code / Cursor / Inspector ──Bearer mgw_…──► app.localtest.me/mcp
```

| Layer | Where | Notes |
|---|---|---|
| Store | `charts/store` | WordPress 6.6 + WooCommerce 9.3, MariaDB 11.4, both on PVCs. A WP-CLI Job installs and configures them idempotently. |
| WooCommerce MCP server | `services/woo-mcp` | Written for this project. Streamable HTTP. Tools: `list_products`, `create_product`, `list_orders` (with `period: today`), `get_order`, `update_order_status`, `get_store_info`. |
| Gateway + dashboard | `services/gateway`, `services/dashboard`, `charts/platform` | MCP client of every registered server and MCP server for all of them. Users, roles, hashed API keys, encrypted upstream credentials, audit log, metrics. |
| Chat agent | `services/gateway/gateway/chat.py` | Gemini tool loop. It is an MCP client of the gateway's own `/mcp`, acting as the logged-in user. |
| Other upstreams | `services/shipping-mcp`, `services/utils-mcp` | Shipping quotes and shipments (it has a `get_order` that collides with WooCommerce's), plus currency, time and arithmetic. |

More detail:
- [System design and tradeoffs](docs/design.md)
- [Threat model](docs/threat-model.md)
- [Stand-out items attempted](docs/standout.md)

---

## Local setup (k3d on WSL2 or Linux)

**Prerequisites:** Docker Engine, [k3d](https://k3d.io) 5.x, Helm 3.14+, kubectl, openssl.
On Windows, run everything inside WSL2 Ubuntu:

```bash
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER   # then: wsl --shutdown, reopen
curl -fsSL https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
sudo snap install kubectl --classic
```

Get a free Gemini API key at https://aistudio.google.com/apikey. The free tier is
rate-limited and never billed.

Then, from the repo root:

```bash
GEMINI_API_KEY=your-key scripts/local-up.sh
```

This creates the `woo` k3d cluster and builds and imports the five images. It then
creates the Secrets with random values (`scripts/secrets.sh`) and installs two
Helm releases:

| Release | Namespace | Chart | Values |
|---|---|---|---|
| `store-main` | `store-main` | `charts/store` | `charts/store/values-local.yaml` |
| `platform` | `platform` | `charts/platform` | `charts/platform/values-local.yaml` |

The first install takes about 5–10 minutes, mostly WooCommerce downloading and
installing. The script prints the URLs and the generated admin passwords at the
end.

### Local domains

Everything uses `*.localtest.me`, a public DNS name whose subdomains all
resolve to `127.0.0.1`. You don't need to edit the hosts file, and the names
behave like real hosts for Ingress routing and cookies. k3d maps host ports
80/443 to Traefik, and WSL2 forwards `localhost` to Windows, so the Windows
browser works too.

| URL | What |
|---|---|
| http://shop.localtest.me/ | storefront (`/wp-admin/` for WooCommerce admin) |
| http://app.localtest.me/ | dashboard |
| http://app.localtest.me/mcp | the gateway's MCP endpoint |

If port 80 is taken, change the k3d port mapping (for example `8080:80`), and set
`store.host`/`publicHost` to include the port.

### WSL notes
- **Keep an Ubuntu terminal open while you use the cluster.** WSL stops the distro
  about a minute after the last session closes, which stops Docker and the k3d cluster.
- If k3s crash-loops after WSL or Docker restarts (*"failed to find interface with
  specified node ip"*), run `k3d cluster stop woo && k3d cluster start woo`.
- The images and cluster need about 10 GB. If drive C: is short, move the distro
  first: `wsl --shutdown; wsl --manage Ubuntu-24.04 --move D:\WSL\Ubuntu-24.04`.
- The Gemini key can be added or changed later with `scripts/secrets.sh set-llm-key`.
  It prompts without echoing and restarts the gateway.

---

## Using it

### Place an order on the storefront
1. Open http://shop.localtest.me/, add a product to the cart and go to checkout.
2. Fill in any name and address, then pay with **Cash on delivery** (enabled by the setup Job).
3. In http://shop.localtest.me/wp-admin/ (user `storeadmin`, password printed by
   `local-up.sh`), go to **WooCommerce → Orders**. The order shows there as *Processing*.

### Dashboard: servers, members, keys, audit
Sign in at http://app.localtest.me/ with the admin email and password printed by
the script.

- **Servers.** WooCommerce, Shipping and Utilities are already registered. Each shows
  Connected / Unreachable / Disabled, the reason when it's unreachable, and its
  tools. Admins can add a server by URL with an optional bearer or header credential,
  edit, disable or remove it, and mark individual tools *admins only*.
- **Members.** Invite a teammate. You get a one-time invite link, since there's
  no mail server. You can also change roles, issue or revoke keys, and deactivate members.
- **API keys.** Any user can generate their own key. It's shown once and stored as an HMAC.
- **Audit log.** Every tool call: who, via which key or the chat, server, tool,
  time, duration and outcome. Includes calls that were denied, rejected (revoked
  key, rate limit) or hit a dead upstream.

### Connect an external MCP client (as user B)
Generate a key on **API keys**, then run:

```bash
claude mcp add --transport http team-gateway http://app.localtest.me/mcp --header "Authorization: Bearer mgw_..."
```

- **Cursor:** add `{"mcpServers":{"team-gateway":{"url":"http://app.localtest.me/mcp","headers":{"Authorization":"Bearer mgw_..."}}}}` to `~/.cursor/mcp.json`.
- **MCP Inspector:** run `npx @modelcontextprotocol/inspector`, choose Streamable HTTP, enter the URL, and use the key as the Bearer token.

The client sees `woocommerce__list_orders`, `shipping__get_order`,
`utilities__convert_currency` and so on. A prompt that needs two upstreams:
*"What did today's orders add up to in EUR?"* (calls `woocommerce__list_orders`
and then `utilities__convert_currency`).

### Chat agent (as user A)
On **Chat**, ask *"Create a product called Gateway Mug priced at 14.50"*. It then
appears on the storefront. Place an order for it by hand, then ask *"What orders
came in today?"*. Each answer lists the tool calls the agent made, with
arguments and results. The calls appear in the audit log under your user with
source `chat`.

---

## Production-like VPS (k3s)

The same charts, with `values-prod.yaml` instead of `values-local.yaml`.

1. **On the VPS** (Ubuntu, 2 vCPU / 4 GB is enough):
   `curl -sfL https://get.k3s.io | sh -`. k3s ships Traefik, the `local-path`
   storage class and NetworkPolicy enforcement.
2. **TLS:** install cert-manager and create a `letsencrypt-prod` ClusterIssuer
   (HTTP-01 solver, Traefik ingress class). See `deploy/cluster-issuer.yaml`.
3. **Images:** `REGISTRY=ghcr.io/<you>/ TAG=1.0.0 PUSH=1 scripts/build-images.sh`.
   Make the packages public or add an `imagePullSecret`.
4. **DNS:** point `app.` and `shop.` at the VPS. Alternatively use
   `app.<ip-with-dashes>.sslip.io`, which needs no DNS setup and works with Let's Encrypt.
5. Edit the hosts in `charts/*/values-prod.yaml`, then:

```bash
export KUBECONFIG=~/.kube/vps.yaml      # copied from /etc/rancher/k3s/k3s.yaml, server address changed
GEMINI_API_KEY=... ADMIN_EMAIL=you@yourdomain scripts/secrets.sh
helm upgrade --install store-main charts/store -n store-main -f charts/store/values-prod.yaml --wait --timeout 15m
helm upgrade --install platform charts/platform -n platform -f charts/platform/values-prod.yaml --wait --timeout 10m
```

What changes between local and production, all through values:

| Concern | Local (`values-local.yaml`) | Production (`values-prod.yaml`) |
|---|---|---|
| Domain | `*.localtest.me` | your domain or `*.<ip>.sslip.io` |
| Scheme / TLS | http, no TLS | https, cert-manager `letsencrypt-prod`, `COOKIE_SECURE=true` |
| Images | `woo-mcp:dev`, imported with `k3d image import` | `ghcr.io/<you>/woo-mcp:1.0.0`, pulled |
| Storage | `local-path`, 2 Gi | `local-path` (or Longhorn/cloud class), 10 Gi |
| Resources | small requests | larger requests and limits for WordPress/MariaDB |
| Secrets | `scripts/secrets.sh` (random) | same script, or create the same Secret names from a secret manager (External Secrets, Sealed Secrets) |

### Upgrades and rollback

Every component is a Helm release, so upgrades and rollbacks use Helm's own history:

```bash
# upgrade: build and push a new tag, then
helm upgrade platform charts/platform -n platform -f charts/platform/values-prod.yaml --set images.tag=1.0.1 --wait
helm upgrade store-main charts/store -n store-main -f charts/store/values-prod.yaml --set images.wooMcp.tag=1.0.1 --wait
helm history platform -n platform
helm rollback platform <REVISION> -n platform --wait
```

- **Gateway and dashboard:** rolling updates with `maxUnavailable: 0` and two gateway
  replicas. The MCP transport is stateless, so clients don't notice a rollout.
  Readiness gates traffic on database connectivity. Schema creation is additive
  and serialized with a Postgres advisory lock.
- **woo-mcp:** stateless. A rollback is just the previous image.
- **WordPress/WooCommerce:** the image tag and `store.woocommerceVersion` are values.
  The setup Job re-runs on every upgrade and is idempotent. Back up first, because
  WooCommerce runs database migrations that `helm rollback` can't undo:
  `kubectl -n store-main exec mariadb-0 -- mariadb-dump -uroot -p"$PW" wordpress > backup.sql`.
  The content PVC has `helm.sh/resource-policy: keep`, so even `helm uninstall`
  doesn't delete store content.

---

## Tests

```bash
cd services/woo-mcp && python -m pytest -q    # OAuth 1.0a signing vs a port of WooCommerce's PHP verifier, tools
cd services/gateway && python -m pytest -q    # SSRF guard, naming, tokens, and an end-to-end run of the DoD control flow
```

The gateway's end-to-end test starts the real upstream servers, the gateway and a
fake LLM over HTTP, then drives them with real MCP clients. It checks namespacing
and collisions, that members can't see admin-only tools, key revocation, disabling
a server, a dead upstream, audit attribution, and the chat loop acting as the user.

### Smoke test against the running cluster

```bash
pip install mcp httpx && scripts/smoke-test.sh        # CHAT=1 scripts/smoke-test.sh also asks the chat agent
```

It goes through the public Ingress only. It signs in as the admin (user A), invites
a member (user B) and issues keys. A creates a product over MCP and the test checks
it's on the storefront. It checks out an order for it with Cash on Delivery through
WooCommerce's Store API, then has B list today's orders through MCP and convert the
total to EUR (a second upstream). It also checks that B can't see or call
admin-only tools, disables a server and checks its tools disappear, revokes B's key
(401), and checks the audit attribution.

**Kill-pod check:** `kubectl -n platform scale deploy/utils-mcp --replicas=0`.
Utilities shows **Unreachable — connection refused or no route to host** in the
dashboard and its tools leave the list, while WooCommerce and Shipping calls keep
working. Scale it back to 1 and it reconnects on the next health sweep.

**NetworkPolicy check:** this command times out from `deploy/utils-mcp` and succeeds from `deploy/gateway`:
`kubectl -n platform exec deploy/utils-mcp -- python -c "import urllib.request; urllib.request.urlopen('http://woo-mcp.store-main.svc.cluster.local:8000/healthz', timeout=5)"`

## How this was built

Built with Claude Code (Claude Opus 5.5) working with me. The code, charts and
docs were written in that collaboration. They were verified by the unit and
end-to-end tests above, and by the smoke test, kill-pod check and NetworkPolicy
check on a local k3d cluster (k3s v1.35). The production values have not yet been
run on a real VPS.
