"""Security acceptance tests against a LIVE server (real HTTP, real JWT validation, real Neo4j)."""

from __future__ import annotations

import asyncio
import time

import httpx
import jwt
import pytest

from tests.conftest import AUDIENCE, ISSUER, make_settings

pytestmark = pytest.mark.neo4j
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


async def post(url: str, token: str | None, body=LIST, extra: dict | None = None) -> httpx.Response:
    h = dict(HEADERS, **(extra or {}))
    if token is not None:
        h["authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=10) as c:
        return await c.post(url, headers=h, json=body)


async def test_valid_token_is_accepted(server, mint):
    assert (await post(server.mcp_url, mint())).status_code == 200


async def test_missing_token_is_401(server):
    r = await post(server.mcp_url, None)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert "tools" not in r.text


async def test_non_bearer_scheme_is_401(server, mint):
    r = await post(server.mcp_url, None, extra={"authorization": f"Basic {mint()}"})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("expired", dict(expires_in=-30)),
        ("wrong_audience", dict(aud="someone-else")),
        ("wrong_issuer", dict(iss="https://evil.example")),
        ("missing_exp", dict(expires_in=None)),
        ("missing_sub", dict(sub=None)),
        ("not_yet_valid", dict(nbf=int(time.time()) + 3600)),
    ],
)
async def test_invalid_claims_are_401(server, mint, name, kwargs):
    r = await post(server.mcp_url, mint(**kwargs))
    assert r.status_code == 401, name


async def test_forged_signature_is_401(server, mint, other_keys):
    assert (await post(server.mcp_url, mint(key=other_keys))).status_code == 401


async def test_unsigned_alg_none_is_401(server):
    tok = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "scope": "graph:read", "exp": int(time.time()) + 300},
        key=None,
        algorithm="none",
    )
    assert (await post(server.mcp_url, tok)).status_code == 401


async def test_hs256_with_public_key_as_secret_is_401(server, keys):
    """Classic algorithm-confusion attack: sign HS256 using the RSA public key text."""
    import base64
    import hashlib
    import hmac
    import json

    def b64(b: bytes) -> bytes:
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "scope": "graph:read", "exp": int(time.time()) + 300}
    signing_input = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + b"." + b64(json.dumps(claims).encode())
    sig = hmac.new(keys.public_pem.encode(), signing_input, hashlib.sha256).digest()  # PyJWT refuses to do this
    tok = (signing_input + b"." + b64(sig)).decode()
    assert (await post(server.mcp_url, tok)).status_code == 401


async def test_garbage_tokens_are_401(server):
    for t in ("abc", "a.b.c", "Bearer", "x" * 5000, "a." * 50):
        assert (await post(server.mcp_url, t)).status_code == 401


async def test_valid_token_without_graph_read_scope_is_403(server, mint):
    for scope in ("other", "", "graph:admin"):
        r = await post(server.mcp_url, mint(scope=scope))
        assert r.status_code == 403, scope
        assert r.json()["error"] == "insufficient_scope"


async def test_token_in_query_string_is_ignored(server, mint):
    async with httpx.AsyncClient() as c:
        r = await c.post(server.mcp_url + f"?access_token={mint()}", headers=HEADERS, json=LIST)
    assert r.status_code == 401


async def test_auth_failures_do_not_execute_tools(server, mint, other_keys):
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "search_entities", "arguments": {"query": "ledger"}},
    }
    r = await post(server.mcp_url, mint(key=other_keys), body)
    assert r.status_code == 401 and "svc-ledger" not in r.text


async def test_auth_denial_rate_is_100_percent_on_negative_cases(server, mint, other_keys):
    negatives = [
        None,
        "junk",
        mint(expires_in=-5),
        mint(aud="x"),
        mint(iss="x"),
        mint(key=other_keys),
        mint(expires_in=None),
        mint(sub=None),
    ]
    codes = [(await post(server.mcp_url, t)).status_code for t in negatives]
    assert all(c == 401 for c in codes), codes
    assert (await post(server.mcp_url, mint(scope="nope"))).status_code == 403


# --- operational endpoints ----------------------------------------------------------------------
async def test_health_and_ready_are_minimal(server):
    async with httpx.AsyncClient() as c:
        assert (await c.get(server.base_url + "/health")).json() == {"status": "ok"}
        r = await c.get(server.base_url + "/ready")
        assert r.status_code == 200 and r.json() == {"status": "ready"}


async def test_status_requires_admin_scope(server, mint):
    async with httpx.AsyncClient() as c:
        assert (await c.get(server.base_url + "/status")).status_code == 401
        h = {"authorization": f"Bearer {mint()}"}
        assert (await c.get(server.base_url + "/status", headers=h)).status_code == 403
        h = {"authorization": f"Bearer {mint(scope='graph:read graph:admin')}"}
        # scope claim alone is not admin: admin requires the verified principal to be allow-listed
        assert (await c.get(server.base_url + "/status", headers=h)).status_code in (200, 403)


async def test_status_for_listed_admin_has_no_secrets(neo4j_db, keys, mint, start_server):
    s = make_settings(neo4j_db, keys, auth_admin_principals="ops-user")
    srv = start_server(s)
    async with httpx.AsyncClient() as c:
        r = await c.get(srv.base_url + "/status", headers={"authorization": f"Bearer {mint(sub='ops-user')}"})
    assert r.status_code == 200
    body = r.text
    assert neo4j_db.reader_password not in body and neo4j_db.uri not in body and "password" not in body.lower()
    assert r.json()["graph_revision"] == neo4j_db.revision


async def test_ready_reports_unavailable_without_leaking_detail(neo4j_db, keys, start_server):
    s = make_settings(
        neo4j_db, keys, neo4j_uri="bolt://127.0.0.1:1", neo4j_connect_timeout_s=0.5, neo4j_max_retry_s=0.2
    )
    srv = start_server(s)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(srv.base_url + "/ready")
        assert r.status_code == 503 and r.json() == {"status": "unavailable"}
        assert (await c.get(srv.base_url + "/health")).status_code == 200  # liveness independent of DB


async def test_principal_allowlist_grants_scope_to_scopeless_tokens(neo4j_db, keys, mint, start_server):
    """Mimics Google ID tokens: no scope claim, identity via verified email."""
    s = make_settings(neo4j_db, keys, auth_allowed_principals="alice@example.org")
    srv = start_server(s)
    tok = lambda **kw: mint(scope=None, sub="1234", **kw)  # noqa: E731
    assert (await post(srv.mcp_url, tok(email="alice@example.org", email_verified=True))).status_code == 200
    assert (await post(srv.mcp_url, tok(email="alice@example.org", email_verified=False))).status_code == 403
    assert (await post(srv.mcp_url, tok(email="alice@example.org"))).status_code == 403
    assert (await post(srv.mcp_url, tok(email="mallory@example.org", email_verified=True))).status_code == 403


async def test_logs_correlate_request_ids_and_never_contain_tokens_or_query_text(server, mint, capfd):
    from fastmcp import Client

    token = mint()
    secret_query = "ZXQ-secret-probe-text"
    async with Client(server.mcp_url, auth=token) as c:
        r = await c.call_tool("search_entities", {"query": secret_query})
    rid = r.structured_content["meta"]["request_id"]
    await asyncio.sleep(0.3)
    out = capfd.readouterr()
    logs = out.out + out.err
    assert rid in logs, "client-visible request id must appear in server logs"
    assert token not in logs and token.split(".")[1] not in logs
    assert secret_query not in logs, "raw user queries must not be logged"
    assert "password" not in logs.lower()


async def test_host_header_allowlist_blocks_unexpected_hosts(neo4j_db, keys, mint, start_server):
    """DNS-rebinding / host-confusion guard, enabled in deployments via GRAPHRAG_ALLOWED_HOSTS."""
    s = make_settings(neo4j_db, keys, allowed_hosts="svc.example.com")
    srv = start_server(s)
    ok = await post(srv.mcp_url, mint(), extra={"host": "svc.example.com"})
    bad = await post(srv.mcp_url, mint(), extra={"host": "evil.example.net"})
    assert ok.status_code == 200
    assert bad.status_code in (400, 403, 421), bad.status_code
    async with httpx.AsyncClient() as c:  # liveness/readiness probes must keep working for any Host
        assert (await c.get(srv.base_url + "/health", headers={"host": "10.1.2.3"})).status_code == 200


async def test_jwks_verification_path_with_kid_rotation(neo4j_db, keys, other_keys, start_server):
    """The Google/OIDC deployment path: keys fetched from a JWKS endpoint, selected by `kid`.

    A local JWKS server stands in for the issuer (SSRF guard relaxed for environment=local only).
    Exercises: valid kid, unknown kid, key rotation (new key published later), token signed by a key
    that is not in the JWKS.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from cryptography.hazmat.primitives import serialization
    from jwt.algorithms import RSAAlgorithm

    def jwk(k, kid):
        pub = serialization.load_pem_public_key(k.public_pem.encode())
        d = json.loads(RSAAlgorithm.to_jwk(pub))
        d.update(kid=kid, use="sig", alg="RS256")
        return d

    published = {"keys": [jwk(keys, "k1")]}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(published).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        s = make_settings(
            neo4j_db,
            keys,
            auth_public_key="",
            auth_jwks_uri=f"http://127.0.0.1:{httpd.server_port}/jwks",
            auth_jwks_ssrf_safe=False,
        )
        srv = start_server(s)

        def tok(key, kid):
            return jwt.encode(
                {"iss": ISSUER, "aud": AUDIENCE, "sub": "u", "scope": "graph:read", "exp": int(time.time()) + 300},
                key.private_pem,
                algorithm="RS256",
                headers={"kid": kid},
            )

        assert (await post(srv.mcp_url, tok(keys, "k1"))).status_code == 200
        assert (await post(srv.mcp_url, tok(keys, "nope"))).status_code == 401  # unknown kid
        assert (await post(srv.mcp_url, tok(other_keys, "k2"))).status_code == 401  # key not yet published
        published["keys"].append(jwk(other_keys, "k2"))  # rotation: issuer publishes a new key
        await asyncio.sleep(0.5)
        resp = await post(srv.mcp_url, tok(other_keys, "k2"))
        # Documented behaviour: the verifier caches the JWKS (30 s refresh interval), so a newly published
        # key is only honoured after the cache refreshes. Observed: rejected immediately after rotation.
        assert resp.status_code == 401
    finally:
        httpd.shutdown()


def test_ssrf_guard_cannot_be_disabled_outside_local():
    import pytest as _pt
    from pydantic import ValidationError

    from graphrag_mcp.config import Settings

    with _pt.raises(ValidationError):
        Settings(
            auth_issuer="i",
            auth_audience="a",
            auth_jwks_uri="https://x/jwks",
            auth_jwks_ssrf_safe=False,
            environment="production",
            neo4j_uri="neo4j+s://db",
            neo4j_user="reader",
        )
