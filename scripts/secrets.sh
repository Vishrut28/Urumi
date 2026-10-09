#!/usr/bin/env bash
# Create the Kubernetes Secrets the charts reference. Random values, generated
# once: re-running keeps existing Secrets (delete one to rotate it).
#
#   scripts/secrets.sh                 # create missing Secrets
#   GEMINI_API_KEY=... scripts/secrets.sh set-llm-key
#
# Env: ADMIN_EMAIL (default admin@example.com), GEMINI_API_KEY (prompted if unset),
#      PLATFORM_NS (platform), STORE_NS (store-main)
set -euo pipefail

PLATFORM_NS=${PLATFORM_NS:-platform}
STORE_NS=${STORE_NS:-store-main}
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}

rand() { openssl rand -hex "${1:-24}"; }
fernet_key() { openssl rand -base64 32 | tr '+/' '-_'; }
exists() { kubectl -n "$1" get secret "$2" >/dev/null 2>&1; }
secret_value() { kubectl -n "$1" get secret "$2" -o "jsonpath={.data.$3}" | base64 -d; }

ensure_namespaces() {
  kubectl create namespace "$PLATFORM_NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl create namespace "$STORE_NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  # The gateway's egress NetworkPolicy reaches woo-mcp only in namespaces with this label.
  kubectl label namespace "$STORE_NS" woo-mcp-gateway/store=true --overwrite >/dev/null
}

read_llm_key() {
  if [ -z "${GEMINI_API_KEY:-}" ] && [ -t 0 ]; then
    read -r -s -p "Gemini API key (from https://aistudio.google.com/apikey; empty to skip): " GEMINI_API_KEY; echo
  fi
  printf '%s' "${GEMINI_API_KEY:-}"
}

create_store_secret() {
  if exists "$STORE_NS" store-secrets; then echo "keep   $STORE_NS/store-secrets"; return; fi
  kubectl -n "$STORE_NS" create secret generic store-secrets \
    --from-literal=db-root-password="$(rand)" \
    --from-literal=db-password="$(rand)" \
    --from-literal=wp-admin-password="$(rand 12)" \
    --from-literal=wc-consumer-key="ck_$(rand 20)" \
    --from-literal=wc-consumer-secret="cs_$(rand 20)" \
    --from-literal=mcp-token="$(rand 32)" >/dev/null
  echo "create $STORE_NS/store-secrets"
}

create_platform_secret() {
  if exists "$PLATFORM_NS" platform-secrets; then echo "keep   $PLATFORM_NS/platform-secrets"; return; fi
  local llm; llm=$(read_llm_key)
  kubectl -n "$PLATFORM_NS" create secret generic platform-secrets \
    --from-literal=session-secret="$(rand 32)" \
    --from-literal=api-key-pepper="$(rand 32)" \
    --from-literal=credentials-key="$(fernet_key)" \
    --from-literal=admin-email="$ADMIN_EMAIL" \
    --from-literal=admin-password="$(rand 10)" \
    --from-literal=postgres-password="$(rand)" \
    --from-literal=llm-api-key="$llm" \
    --from-literal=shipping-api-key="$(rand 24)" \
    --from-literal=upstream-woocommerce-token="$(secret_value "$STORE_NS" store-secrets mcp-token)" >/dev/null
  echo "create $PLATFORM_NS/platform-secrets"
}

case "${1:-create}" in
  create)
    ensure_namespaces
    create_store_secret
    create_platform_secret
    ;;
  set-llm-key)
    key=$(read_llm_key)
    kubectl -n "$PLATFORM_NS" patch secret platform-secrets --type merge -p "{\"stringData\":{\"llm-api-key\":\"$key\"}}"
    kubectl -n "$PLATFORM_NS" rollout restart deployment/gateway
    ;;
  *) echo "usage: $0 [create|set-llm-key]" >&2; exit 2 ;;
esac
