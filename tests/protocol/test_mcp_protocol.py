"""Real-protocol tests over Streamable HTTP against a live server and live Neo4j.

Three client stacks drive the *same* server and must see identical tool schemas and results:
  1. FastMCP's Client,
  2. the official ``mcp`` SDK ClientSession (separate client code path),
  3. a hand-written raw JSON-RPC-over-HTTP client (no MCP library at all).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import httpx2
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.neo4j
EXPECTED_TOOLS = {"search_entities", "get_entity", "find_relationship_paths", "retrieve_graph_context"}


# --- client adapters ---------------------------------------------------------------------------
async def fastmcp_call(url, token, tool, args):
    async with Client(url, auth=token) as c:
        r = await c.call_tool(tool, args)
        return r.structured_content


async def fastmcp_tools(url, token):
    async with Client(url, auth=token) as c:
        return {t.name: t.input_schema for t in await c.list_tools()}


async def sdk_call(url, token, tool, args):
    async with httpx2.AsyncClient(headers={"authorization": f"Bearer {token}"}) as h:
        async with streamable_http_client(url, http_client=h) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(tool, args)
                assert not res.is_error, res
                return res.structured_content


async def sdk_tools(url, token):
    async with httpx2.AsyncClient(headers={"authorization": f"Bearer {token}"}) as h:
        async with streamable_http_client(url, http_client=h) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name: t.input_schema for t in (await s.list_tools()).tools}


class RawClient:
    """Minimal JSON-RPC client: proves the wire contract without any MCP library."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.h = (
            url,
            {
                "authorization": f"Bearer {token}",
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
        )

    async def rpc(self, method: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                self.url, headers=self.h, json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}
            )
        assert r.status_code == 200, r.text
        text = r.text
        if r.headers["content-type"].startswith("text/event-stream"):
            text = next(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
        return json.loads(text)


async def raw_call(url, token, tool, args):
    msg = await RawClient(url, token).rpc("tools/call", {"name": tool, "arguments": args})
    assert not msg["result"].get("isError"), msg
    return msg["result"]["structuredContent"]


async def raw_tools(url, token):
    msg = await RawClient(url, token).rpc("tools/list")
    return {t["name"]: t["inputSchema"] for t in msg["result"]["tools"]}


CLIENTS = {"fastmcp": (fastmcp_call, fastmcp_tools), "sdk": (sdk_call, sdk_tools), "raw": (raw_call, raw_tools)}


# --- tests -------------------------------------------------------------------------------------
@pytest.mark.parametrize("client", CLIENTS)
async def test_initialize_and_discover_tools(server, mint, client):
    schemas = await CLIENTS[client][1](server.mcp_url, mint())
    assert set(schemas) == EXPECTED_TOOLS
    assert all(s["type"] == "object" for s in schemas.values())


async def test_all_clients_see_identical_schemas(server, mint):
    got = [await CLIENTS[c][1](server.mcp_url, mint()) for c in CLIENTS]
    assert got[0] == got[1] == got[2]


async def test_schema_bounds_are_published(server, mint):
    s = await fastmcp_tools(server.mcp_url, mint())
    assert s["search_entities"]["properties"]["limit"]["maximum"] == 10
    assert s["find_relationship_paths"]["properties"]["max_hops"]["maximum"] == 4
    assert s["retrieve_graph_context"]["properties"]["max_chars"]["minimum"] == 500
    assert s["get_entity"]["properties"]["neighbor_limit"]["maximum"] == 20


CALLS = [
    ("search_entities", {"query": "ledger service", "limit": 3}),
    ("get_entity", {"entity_id": "svc-ledger-service", "neighbor_limit": 4}),
    (
        "find_relationship_paths",
        {
            "source_entity_id": "feat-gift-cards",
            "target_entity_id": "svc-ledger-service",
            "max_hops": 3,
            "max_paths": 2,
        },
    ),
    ("retrieve_graph_context", {"query": "How is Gift Cards connected to the Ledger Service?", "top_k": 3}),
]


def stable(d):
    d = json.loads(json.dumps(d))
    d["meta"].pop("request_id", None)
    d["meta"].pop("latency_ms", None)
    return d


@pytest.mark.parametrize("tool,args", CALLS)
async def test_clients_get_identical_results(server, mint, tool, args):
    results = [stable(await CLIENTS[c][0](server.mcp_url, mint(), tool, args)) for c in CLIENTS]
    assert results[0] == results[1] == results[2]
    assert results[0]["meta"]["graph_revision"].startswith("demo-")


async def test_tool_semantics_over_the_wire(server, mint):
    t = mint()
    s = await fastmcp_call(server.mcp_url, t, *CALLS[0])
    assert s["entities"][0]["id"] == "svc-ledger-service" and s["entities"][0]["evidence"]
    p = await fastmcp_call(server.mcp_url, t, *CALLS[2])
    assert p["paths"] and p["paths"][0]["node_ids"][0] == "feat-gift-cards"
    c = await fastmcp_call(server.mcp_url, t, *CALLS[3])
    assert c["sufficient_evidence"] and all(sn["evidence"] for sn in c["snippets"])


@pytest.mark.parametrize(
    "args,code",
    [
        ({"entity_id": "svc-missing"}, "NOT_FOUND"),
        ({"entity_id": "bad id; DROP"}, "INVALID_ARGUMENT"),
        ({"entity_id": "x" * 500}, "INVALID_ARGUMENT"),
        ({"entity_id": "svc-ledger-service", "neighbor_limit": 99}, "INVALID_ARGUMENT"),
        ({}, "INVALID_ARGUMENT"),
        ({"entity_id": 5}, "INVALID_ARGUMENT"),
    ],
)
async def test_errors_are_typed_and_carry_request_id(server, mint, args, code):
    async with Client(server.mcp_url, auth=mint()) as c:
        with pytest.raises(ToolError) as e:
            await c.call_tool("get_entity", args)
    body = json.loads(str(e.value))
    assert body["code"] == code and body["request_id"] and "Traceback" not in body["message"]
    assert "pydantic" not in body["message"].lower() and "input_value" not in body["message"]


async def test_limits_rejected_over_wire_before_any_work(server, mint):
    async with Client(server.mcp_url, auth=mint()) as c:
        for tool, args in [
            ("find_relationship_paths", {"source_entity_id": "a", "target_entity_id": "b", "max_hops": 5}),
            ("find_relationship_paths", {"source_entity_id": "a", "target_entity_id": "b", "max_paths": 11}),
            ("search_entities", {"query": "x" * 201}),
            ("search_entities", {"query": "ok", "labels": ["NotAKind"]}),
            ("retrieve_graph_context", {"query": "ok", "max_chars": 20000}),
        ]:
            with pytest.raises(ToolError) as e:
                await c.call_tool(tool, args)
            assert json.loads(str(e.value))["code"] == "INVALID_ARGUMENT", (tool, args)


async def test_unknown_tool_and_malformed_rpc_do_not_crash_server(server, mint):
    raw = RawClient(server.mcp_url, mint())
    msg = await raw.rpc("tools/call", {"name": "run_cypher", "arguments": {"query": "MATCH (n) DETACH DELETE n"}})
    assert "error" in msg or msg["result"].get("isError")
    async with httpx.AsyncClient() as c:
        r = await c.post(server.mcp_url, headers=raw.h, content=b"{not json")
        assert r.status_code in (200, 400)
    async with httpx.AsyncClient() as c:
        assert (await c.get(server.base_url + "/health")).status_code == 200


async def test_no_unrestricted_cypher_tool_is_exposed(server, mint):
    names = set(await fastmcp_tools(server.mcp_url, mint()))
    assert not {n for n in names if "cypher" in n or "query" in n and n != "search_entities" or "write" in n}


async def test_concurrent_calls_from_many_clients(server, mint):
    t = mint()
    results = await asyncio.gather(
        *(fastmcp_call(server.mcp_url, t, "search_entities", {"query": "service", "limit": 5}) for _ in range(12))
    )
    assert all(r["entities"] == results[0]["entities"] for r in results)
