#!/usr/bin/env bash
# Run scripts/smoke_test.py against the local cluster, reading the admin login from its Secret.
# Needs python3 with `pip install mcp httpx` (a venv is fine). CHAT=1 also exercises the chat agent.
set -euo pipefail
cd "$(dirname "$0")/.."
ADMIN_EMAIL=$(kubectl -n platform get secret platform-secrets -o jsonpath='{.data.admin-email}' | base64 -d)
ADMIN_PASSWORD=$(kubectl -n platform get secret platform-secrets -o jsonpath='{.data.admin-password}' | base64 -d)
export ADMIN_EMAIL ADMIN_PASSWORD
exec "${PYTHON:-python3}" scripts/smoke_test.py
