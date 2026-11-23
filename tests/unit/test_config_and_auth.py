from __future__ import annotations

import pytest
from pydantic import ValidationError

from graphrag_mcp.config import Settings

BASE = dict(auth_issuer="i", auth_audience="a", auth_public_key="k")


def test_config_fails_closed_without_auth_settings():
    with pytest.raises(ValidationError):
        Settings(auth_public_key="k")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(auth_issuer="i", auth_audience="a")  # no key and no jwks


def test_config_requires_exactly_one_key_source():
    with pytest.raises(ValidationError):
        Settings(**BASE, auth_jwks_uri="https://x/jwks")  # type: ignore[arg-type]


def test_non_local_requires_tls_and_non_admin_user():
    with pytest.raises(ValidationError):
        Settings(**BASE, environment="production", neo4j_uri="bolt://db:7687", neo4j_user="r")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(**BASE, environment="production", neo4j_uri="neo4j+s://db", neo4j_user="neo4j")  # type: ignore[arg-type]
    Settings(**BASE, environment="production", neo4j_uri="neo4j+s://db", neo4j_user="reader")  # type: ignore[arg-type]


def test_no_auth_disable_switch_exists():
    assert not [f for f in Settings.model_fields if "disable" in f or "dev_auth" in f or "insecure" in f]
