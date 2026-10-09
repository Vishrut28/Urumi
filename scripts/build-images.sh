#!/usr/bin/env bash
# Build every image. With PUSH=1 and REGISTRY=ghcr.io/you/ it tags and pushes (prod);
# otherwise it imports them into the local k3d cluster.
set -euo pipefail
cd "$(dirname "$0")/.."
TAG=${TAG:-dev}
REGISTRY=${REGISTRY:-}
CLUSTER=${CLUSTER:-woo}

build() { # name context [dockerfile]
  local ref="${REGISTRY}$1:$TAG"
  echo "==> $ref"
  docker build -t "$ref" -f "${3:-$2/Dockerfile}" "$2"
  if [ "${PUSH:-0}" = 1 ]; then docker push "$ref"; fi
  REFS+=("$ref")
}

REFS=()
build woo-mcp services/woo-mcp
build utils-mcp services/utils-mcp
build shipping-mcp services/shipping-mcp
build dashboard services/dashboard
build gateway . services/gateway/Dockerfile

if [ "${PUSH:-0}" != 1 ]; then
  k3d image import -c "$CLUSTER" "${REFS[@]}"
fi
