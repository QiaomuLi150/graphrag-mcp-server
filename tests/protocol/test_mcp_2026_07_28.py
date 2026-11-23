"""MCP 2026-07-28 conformance, exercised on the raw wire (no MCP library) against a live server."""

from __future__ import annotations

import os

import httpx
import pytest
from eval.two_instance import META, scenario
from fastmcp import Client

pytestmark = pytest.mark.neo4j


def h(token: str, method: str, version: str = "2026-07-28", name: str | None = None) -> dict[str, str]:
    d = {
        "authorization": f"Bearer {token}",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": version,
        "mcp-method": method,
    }
    if name:
        d["mcp-name"] = name
    return d


def rpc(server, token, method, params=None, meta=META, **kw):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {**(params or {}), **({"_meta": meta} if meta is not None else {})},
    }
    return httpx.post(server.mcp_url, headers=h(token, method, **kw), json=body, timeout=15)


def test_server_discover_without_initialize_or_session(server, mint):
    r = rpc(server, mint(), "server/discover")
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["supportedVersions"] == ["2026-07-28"]
    assert res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "graphrag-mcp"
    assert "tools" in res["capabilities"] and "instructions" in res
    assert "mcp-session-id" not in {k.lower() for k in r.headers}


def test_tools_list_and_call_are_self_contained_requests(server, mint):
    t = mint()
    r = rpc(server, t, "tools/list")
    assert r.status_code == 200 and {x["name"] for x in r.json()["result"]["tools"]} == {
        "search_entities",
        "get_entity",
        "find_relationship_paths",
        "retrieve_graph_context",
    }
    r = rpc(
        server,
        t,
        "tools/call",
        {"name": "search_entities", "arguments": {"query": "ledger", "limit": 1}},
        name="search_entities",
    )
    assert r.status_code == 200 and not r.json()["result"].get("isError")
    assert "mcp-session-id" not in {k.lower() for k in r.headers}


def test_request_level_metadata_is_required(server, mint):
    r = rpc(server, mint(), "tools/list", meta=None)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32602
    partial = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    r = rpc(server, mint(), "tools/list", meta=partial)
    assert (
        r.status_code == 400
        and "clientCapabilities" in r.json()["error"]["message"]
        or "clientInfo" in r.json()["error"]["message"]
    )


def test_unsupported_version_is_rejected_with_supported_list(server, mint):
    bad = {**META, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}
    r = rpc(server, mint(), "server/discover", meta=bad, version="2099-01-01")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == -32022 and err["data"]["supported"] == ["2026-07-28"]


def test_auth_still_enforced_on_modern_wire(server):
    assert rpc(server, "not-a-token", "server/discover").status_code == 401


async def test_fastmcp_client_negotiates_modern_protocol(server, mint):
    async with Client(server.mcp_url, auth=mint()) as c:
        assert c.protocol_version == "2026-07-28"
        assert len(await c.list_tools()) == 4


async def test_legacy_handshake_clients_still_work_statelessly(server, mint):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"authorization": f"Bearer {mint()}"}) as hc:
        async with streamable_http_client(server.mcp_url, http_client=hc) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            assert s.protocol_version == "2025-11-25"  # compatibility path, not the primary protocol
            assert not (await s.call_tool("search_entities", {"query": "ledger"})).is_error


async def test_two_processes_without_sticky_sessions(settings, keys, mint):
    env = dict(
        os.environ,
        GRAPHRAG_NEO4J_URI=settings.neo4j_uri,
        GRAPHRAG_NEO4J_USER=settings.neo4j_user,
        GRAPHRAG_NEO4J_PASSWORD=settings.neo4j_password.get_secret_value(),
        GRAPHRAG_NEO4J_DATABASE=settings.neo4j_database,
        GRAPHRAG_AUTH_ISSUER=settings.auth_issuer,
        GRAPHRAG_AUTH_AUDIENCE=settings.auth_audience,
        GRAPHRAG_AUTH_PUBLIC_KEY=keys.public_pem,
    )
    rep = await scenario(env, mint(expires_in=600))
    assert rep["verdict"], rep
    assert rep["client_negotiated_protocol"] == "2026-07-28"
    assert rep["tool_calls_served"]["A"] > 0 and rep["tool_calls_served"]["B"] > 0


def test_advertised_capabilities_match_supported_functionality(server, mint):
    caps = rpc(server, mint(), "server/discover").json()["result"]["capabilities"]
    assert set(caps) == {"tools"}, caps  # no prompts/resources/logging/extensions are implemented
    assert not caps["tools"].get("listChanged")
    # Unadvertised list methods may answer, but only with empty collections: nothing is implemented.
    assert rpc(server, mint(), "prompts/list").json()["result"]["prompts"] == []
    assert rpc(server, mint(), "resources/list").json()["result"]["resources"] == []
    r = rpc(server, mint(), "subscriptions/listen")
    assert r.status_code in (400, 404) or "error" in r.json()
