"""Prometheus metrics, served on /metrics (cluster-internal: the Ingress does not route it)."""

from prometheus_client import Counter, Gauge, Histogram

TOOL_CALLS = Counter("gateway_tool_calls_total", "Tool calls by upstream, source and outcome", ["server", "source", "outcome"])
TOOL_SECONDS = Histogram(
    "gateway_tool_call_seconds", "Tool call latency through the gateway", ["server"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60)
)
UPSTREAM_UP = Gauge("gateway_upstream_up", "1 if the last health probe of the upstream succeeded", ["server"])
PROBE_SECONDS = Histogram("gateway_upstream_probe_seconds", "Duration of upstream health probes", ["server"])
AUTH_FAILURES = Counter("gateway_auth_failures_total", "Rejected MCP requests by reason", ["reason"])
CHAT_TURNS = Counter("gateway_chat_turns_total", "Chat agent turns by outcome", ["outcome"])
