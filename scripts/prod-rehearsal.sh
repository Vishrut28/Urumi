#!/usr/bin/env bash
# Rehearse the production deployment on a separate local k3s cluster (k3d "woo-prod"):
# values-prod.yaml for both charts, HTTPS through cert-manager, images pulled from a
# registry, plus deploy/rehearsal/*.yaml for the three things a laptop cannot have
# (public DNS -> *.127-0-0-1.sslip.io, a public registry -> a local one, Let's Encrypt
# -> a private CA). The dev cluster "woo" is stopped while this runs (ports 80/443).
#
#   scripts/prod-rehearsal.sh          bring it up (or start it again if it was stopped)
#   scripts/prod-rehearsal.sh stop     stop woo-prod (keeps it) and start the dev cluster woo
#   scripts/prod-rehearsal.sh down     delete woo-prod and start woo
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-up}" in
  down) k3d cluster delete woo-prod || true; k3d cluster start woo && kubectl config use-context k3d-woo; exit 0 ;;
  stop) k3d cluster stop woo-prod || true; k3d cluster start woo && kubectl config use-context k3d-woo; exit 0 ;;
esac

# Reuse the dev cluster's Gemini key without printing it.
LLM=$(kubectl --context k3d-woo -n platform get secret platform-secrets -o jsonpath='{.data.llm-api-key}' 2>/dev/null | base64 -d || true)
k3d cluster stop woo 2>/dev/null || true

k3d registry list 2>/dev/null | grep -q k3d-woo-registry || k3d registry create woo-registry --port 5050
if ! k3d cluster list 2>/dev/null | grep -q '^woo-prod '; then
  k3d cluster create woo-prod --registry-use k3d-woo-registry:5050 -p "80:80@loadbalancer" -p "443:443@loadbalancer"
else
  k3d cluster start woo-prod
fi
kubectl config use-context k3d-woo-prod

helm repo add jetstack https://charts.jetstack.io --force-update >/dev/null
helm upgrade --install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true --wait --timeout 5m
kubectl apply -f deploy/rehearsal/ca-issuer.yaml
kubectl -n cert-manager wait --for=condition=Ready certificate/rehearsal-ca --timeout=120s

REGISTRY=localhost:5050/ TAG=1.0.0 PUSH=1 scripts/build-images.sh

GEMINI_API_KEY="$LLM" ADMIN_EMAIL=admin@example.com scripts/secrets.sh

helm upgrade --install store-main charts/store -n store-main \
  -f charts/store/values-prod.yaml -f deploy/rehearsal/values-store.yaml --wait --timeout 15m
helm upgrade --install platform charts/platform -n platform \
  -f charts/platform/values-prod.yaml -f deploy/rehearsal/values-platform.yaml --wait --timeout 10m

kubectl -n cert-manager get secret rehearsal-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > deploy/rehearsal/ca.crt
cat <<MSG

Production rehearsal is up (trust deploy/rehearsal/ca.crt, or curl --cacert it):
  https://shop.127-0-0-1.sslip.io/
  https://app.127-0-0-1.sslip.io/       MCP: https://app.127-0-0-1.sslip.io/mcp
  admin: $(kubectl -n platform get secret platform-secrets -o jsonpath='{.data.admin-email}' | base64 -d)
MSG
