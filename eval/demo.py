"""Local MCP client demonstration: discover tools and run real GraphRAG requests, printing a transcript."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from fastmcp import Client
from fastmcp.exceptions import ToolError

STEPS = [
    ("Find an entity by (partial) name", "search_entities", {"query": "ledger", "limit": 3}),
    ("Inspect it with attributed neighbours", "get_entity", {"entity_id": "svc-ledger-service", "neighbor_limit": 3}),
    (
        "How are two things connected (multi-hop)?",
        "find_relationship_paths",
        {
            "source_entity_id": "feat-gift-cards",
            "target_entity_id": "svc-ledger-service",
            "max_hops": 3,
            "max_paths": 2,
        },
    ),
    (
        "Ask in natural language, get evidence",
        "retrieve_graph_context",
        {"query": "How is Partner Gateway Service related to Ledger Service?", "top_k": 3, "max_chars": 800},
    ),
    (
        "Unsupported question -> abstains",
        "retrieve_graph_context",
        {"query": "What is the airspeed of an unladen swallow?"},
    ),
    ("Bad input -> typed error", "get_entity", {"entity_id": "svc-does-not-exist"}),
]


async def main_async(url: str, token: str) -> None:
    async with Client(url, auth=token) as c:
        tools = await c.list_tools()
        print(f"Discovered {len(tools)} tools at {url}: {', '.join(sorted(t.name for t in tools))}\n")
        for title, name, args in STEPS:
            print(f"## {title}\n> {name}({json.dumps(args)})")
            try:
                res = (await c.call_tool(name, args)).structured_content
                text = json.dumps(res)
                print(text[:700] + (" …[truncated for display]" if len(text) > 700 else ""), "\n")
            except ToolError as e:
                print("ERROR:", str(e), "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("MCP_URL", "http://127.0.0.1:28080/mcp"))
    ap.add_argument("--token-env", default="GRAPHRAG_TOKEN")
    a = ap.parse_args()
    asyncio.run(main_async(a.url, os.environ[a.token_env]))


if __name__ == "__main__":
    main()
