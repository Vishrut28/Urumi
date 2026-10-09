#!/usr/bin/env bash
# One command from nothing to a working local platform on k3d.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! k3d cluster list 2>/dev/null | grep -q '^woo '; then
  k3d cluster create --config deploy/k3d.yaml
fi
kubectl config use-context k3d-woo >/dev/null

scripts/build-images.sh
scripts/secrets.sh

helm upgrade --install store-main charts/store -n store-main \
  -f charts/store/values-local.yaml --wait --timeout 15m
helm upgrade --install platform charts/platform -n platform \
  -f charts/platform/values-local.yaml --wait --timeout 10m

# Restart the gateway when images were rebuilt under the same tag.
kubectl -n platform rollout restart deployment/gateway deployment/dashboard deployment/utils-mcp deployment/shipping-mcp >/dev/null
kubectl -n store-main rollout restart deployment/woo-mcp >/dev/null
kubectl -n platform rollout status deployment/gateway --timeout 180s

cat <<MSG

Storefront:  http://shop.localtest.me/
Dashboard:   http://app.localtest.me/
MCP:         http://app.localtest.me/mcp
Admin login: $(kubectl -n platform get secret platform-secrets -o jsonpath='{.data.admin-email}' | base64 -d) / $(kubectl -n platform get secret platform-secrets -o jsonpath='{.data.admin-password}' | base64 -d)
WP admin:    storeadmin / $(kubectl -n store-main get secret store-secrets -o jsonpath='{.data.wp-admin-password}' | base64 -d)
MSG
