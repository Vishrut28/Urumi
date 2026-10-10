#!/usr/bin/env bash
# Upgrade and roll back the platform with Helm while traffic keeps flowing.
#
#   scripts/upgrade-rollback-demo.sh            (local k3d cluster from local-up.sh)
#
# 1. Tags the current images as a new version (1.0.1 by default) and upgrades
#    the platform release to it: rolling update, maxUnavailable 0.
# 2. Rolls back to the previous Helm revision.
# 3. Attempts a broken upgrade (an image tag that does not exist) with --atomic:
#    Helm notices the rollout never becomes ready and rolls back by itself.
# A background probe calls the gateway health endpoint twice a second the whole
# time and reports how many requests failed.
set -euo pipefail
cd "$(dirname "$0")/.."
NEW=${NEW_TAG:-1.0.1}
VALUES=charts/platform/values-local.yaml
URL=${PROBE_URL:-http://app.localtest.me/healthz}

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
images() { kubectl -n platform get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.spec.template.spec.containers[0].image}{"  "}{end}'; echo; }

probe_log=$(mktemp)
( while true; do curl -s -o /dev/null -m 2 -w '%{http_code}\n' "$URL" >> "$probe_log" || echo 000 >> "$probe_log"; sleep 0.5; done ) &
probe_pid=$!
trap 'kill $probe_pid 2>/dev/null || true' EXIT

step "current release"
helm -n platform history platform --max 3
images

step "build $NEW: tag the current images and load them into k3d"
for img in gateway dashboard utils-mcp shipping-mcp; do docker tag "$img:dev" "$img:$NEW"; done
k3d image import -c woo gateway:$NEW dashboard:$NEW utils-mcp:$NEW shipping-mcp:$NEW >/dev/null
echo "imported gateway, dashboard, utils-mcp, shipping-mcp :$NEW"

step "upgrade platform to $NEW (rolling, zero downtime)"
helm upgrade platform charts/platform -n platform -f "$VALUES" --set images.tag="$NEW" --wait --timeout 5m
images

step "roll back to the previous revision"
prev=$(( $(helm -n platform history platform -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["revision"])') - 1 ))
helm rollback platform "$prev" -n platform --wait --timeout 5m
images

step "broken upgrade with --atomic (image tag that does not exist)"
if helm upgrade platform charts/platform -n platform -f "$VALUES" --set images.tag=does-not-exist --atomic --wait --timeout 90s; then
  echo "unexpected: broken upgrade succeeded"; exit 1
else
  echo "upgrade failed as expected; Helm rolled back automatically"
fi
images
helm -n platform history platform --max 5

kill $probe_pid 2>/dev/null || true
total=$(wc -l < "$probe_log"); bad=$(grep -vc '^200$' "$probe_log" || true)
step "traffic during the whole exercise: $total health requests, $bad failed"
