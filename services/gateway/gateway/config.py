"""Gateway settings. Every secret arrives as an environment variable from a Kubernetes Secret."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    database_url: str = "sqlite+aiosqlite:///./gateway.db"

    # Secrets (Kubernetes Secret -> env). No defaults that would work in a cluster.
    session_secret: str = Field(default="", description="HMAC key for dashboard sessions and chat tokens")
    api_key_pepper: str = Field(default="", description="HMAC key API keys are hashed with")
    credentials_key: str = Field(default="", description="Fernet key that encrypts upstream credentials at rest")
    admin_email: str = ""
    admin_password: str = ""

    # LLM for the chat agent: any OpenAI-compatible endpoint. Default is Gemini's.
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    llm_model: str = "gemini-3.5-flash"
    llm_reasoning_effort: str = "low"  # "" to omit; lower is much faster on Gemini 3.x
    llm_api_key: str = ""
    llm_timeout_seconds: float = 60.0
    chat_max_steps: int = 8
    chat_max_tools: int = 40  # above this, the agent gets search/call meta-tools instead

    # Upstreams
    health_interval_seconds: float = 10.0
    health_timeout_seconds: float = 5.0
    default_call_timeout_seconds: float = 20.0
    seed_servers: str = "[]"  # JSON list, see seed.py
    # Hosts that may resolve to private addresses (in-cluster upstreams). Glob patterns.
    ssrf_allowed_internal_hosts: str = "*.svc.cluster.local"
    ssrf_allow_all: bool = False  # local experiments only

    # Team and limits
    rate_limit_calls_per_minute: int = 60
    public_url: str = "http://app.localtest.me"  # shown to users as the gateway URL
    self_mcp_url: str = "http://127.0.0.1:8080/mcp"  # where the chat agent reaches this gateway's MCP endpoint
    cookie_secure: bool = False

    # Multi-store provisioning and hosted MCP servers; off unless the chart enables them.
    provisioner_enabled: bool = False
    provisioner_max_stores_per_user: int = 2
    provisioner_timeout_seconds: int = 900
    provisioner_store_domain: str = "localtest.me"
    provisioner_store_scheme: str = "http"
    provisioner_gateway_namespace: str = "platform"
    provisioner_ingress_class: str = "traefik"
    provisioner_store_image: str = "woo-mcp"
    provisioner_store_image_tag: str = "dev"
    provisioner_chart_path: str = "/charts/store"
    provisioner_store_values_file: str = ""  # extra values for every provisioned store (e.g. TLS in prod)
    provisioner_hosted_timeout_seconds: int = 300

    def require_secrets(self) -> None:
        missing = [n for n in ("session_secret", "api_key_pepper", "credentials_key") if not getattr(self, n)]
        if missing:
            raise RuntimeError(f"missing required secrets: {', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
