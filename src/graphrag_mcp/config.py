"""Typed environment configuration. All settings use the ``GRAPHRAG_`` prefix."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GRAPHRAG_", env_file=None, extra="ignore")

    # --- Neo4j (read-only principal only; the seeder uses separate credentials) ---
    neo4j_uri: str = "bolt://127.0.0.1:27687"
    neo4j_user: str = "graphrag_reader"
    neo4j_password: SecretStr = SecretStr("")
    neo4j_database: str = "demo"
    neo4j_connect_timeout_s: float = 5.0
    neo4j_max_retry_s: float = 3.0
    neo4j_pool_size: int = 20

    # --- Request budgets ---
    query_timeout_s: float = 3.0  # server-side Neo4j transaction timeout
    request_deadline_s: float = 8.0  # whole tool call
    max_response_bytes: int = 64_000
    max_concurrency: int = 16  # in-flight graph calls per instance
    rate_limit_per_minute: int = 120  # per principal, per instance (in-memory)

    # --- Auth (JWT bearer; verified in-app even behind a host's own access check) ---
    auth_issuer: str = ""
    auth_audience: str = ""
    auth_jwks_uri: str = ""  # e.g. https://www.googleapis.com/oauth2/v3/certs
    auth_jwks_ssrf_safe: bool = True  # HTTPS + public-address only; may be False ONLY when environment=local
    auth_public_key: str = ""
    auth_public_key_file: str = ""  # path to a PEM file (alternative to auth_public_key)  # PEM; alternative to JWKS (used by local/test issuers)
    auth_algorithm: str = "RS256"
    # Tokens without a scope claim (e.g. Google ID tokens) get graph:read only if the
    # principal (email or sub) appears here. Comma-separated.
    auth_allowed_principals: str = ""
    auth_admin_principals: str = ""  # may call /status

    # --- Server ---
    host: str = "0.0.0.0"  # noqa: S104  # nosec B104  (container bind; exposure is controlled by the port mapping (compose binds 127.0.0.1))
    port: int = Field(default=8080)
    mcp_path: str = "/mcp"
    allowed_hosts: str = ""  # extra Host header values, comma-separated (e.g. the run.app host)
    environment: str = "local"  # local | staging | production
    service_version: str = "0.1.0"
    git_sha: str = "unknown"
    cloud_revision: str = Field(default="", validation_alias="K_REVISION")
    log_level: str = "INFO"
    graph_revision_ttl_s: float = 60.0

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.auth_public_key_file and not self.auth_public_key:
            from pathlib import Path

            self.auth_public_key = Path(self.auth_public_key_file).read_text()
        if not self.auth_issuer or not self.auth_audience:
            raise ValueError("GRAPHRAG_AUTH_ISSUER and GRAPHRAG_AUTH_AUDIENCE are required (fail closed)")
        if bool(self.auth_jwks_uri) == bool(self.auth_public_key):
            raise ValueError("set exactly one of GRAPHRAG_AUTH_JWKS_URI or GRAPHRAG_AUTH_PUBLIC_KEY")
        if self.environment != "local" and self.neo4j_user.lower() == "neo4j":
            raise ValueError("the server must not run with the Neo4j admin user outside local")
        if self.environment != "local" and not self.neo4j_uri.startswith(("neo4j+s://", "bolt+s://")):
            raise ValueError("non-local environments require a TLS Neo4j URI (neo4j+s:// or bolt+s://)")
        if self.environment != "local" and not self.auth_jwks_ssrf_safe:
            raise ValueError("auth_jwks_ssrf_safe may only be disabled in the local environment")
        return self

    def principals(self, raw: str) -> frozenset[str]:
        return frozenset(p.strip().casefold() for p in raw.split(",") if p.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
