"""Black-box smoke test for any deployment (local container or any hosted URL).

Runs from OUTSIDE the server: authenticate, initialise a session, list tools, query a known entity and
a known multi-hop path, query an absent path, send malformed input, and check request ids.
The token is read from an environment variable; the printed transcript never contains the token and
redacts the endpoint host.

    GRAPHRAG_SMOKE_TOKEN=... python -m eval.smoke --url https://HOST/mcp --label cloud-run --out eval/results/smoke.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

EXPECTED = {"search_entities", "get_entity", "find_relationship_paths", "retrieve_graph_context"}


async def run(url: str, token: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []

    def rec(name: str, ok: bool, **detail: Any) -> None:
        steps.append({"step": name, "pass": bool(ok), **detail})

    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    async with httpx.AsyncClient(timeout=30) as h:
        r = await h.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"accept": "application/json, text/event-stream", "content-type": "application/json"},
        )
        rec("unauthenticated_request_rejected", r.status_code in (401, 403), http_status=r.status_code)
        r = await h.get(base + "/health")
        rec("health", r.status_code == 200, http_status=r.status_code)
    async with Client(url, auth=token) as c:
        t0 = time.perf_counter()
        tools = await c.list_tools()
        rec(
            "list_tools",
            {t.name for t in tools} == EXPECTED,
            tools=sorted(t.name for t in tools),
            ms=round(1000 * (time.perf_counter() - t0)),
        )
        res = (await c.call_tool("search_entities", {"query": "ledger service", "limit": 2})).structured_content
        rec(
            "known_entity",
            res["entities"][0]["id"] == "svc-ledger-service",
            top=res["entities"][0]["id"],
            request_id=res["meta"]["request_id"],
            graph_revision=res["meta"]["graph_revision"],
        )
        res = (
            await c.call_tool(
                "find_relationship_paths",
                {
                    "source_entity_id": "feat-gift-cards",
                    "target_entity_id": "svc-ledger-service",
                    "max_hops": 3,
                    "max_paths": 3,
                },
            )
        ).structured_content
        rec(
            "known_multihop_path",
            bool(res["paths"]) and len(res["paths"][0]["relationship_ids"]) == 3,
            hops=len(res["paths"][0]["relationship_ids"]) if res["paths"] else None,
            request_id=res["meta"]["request_id"],
        )
        res = (
            await c.call_tool(
                "find_relationship_paths",
                {
                    "source_entity_id": "svc-legacy-archive-tool",
                    "target_entity_id": "svc-ledger-service",
                    "max_hops": 4,
                },
            )
        ).structured_content
        rec("absent_path_returns_empty", res["paths"] == [], request_id=res["meta"]["request_id"])
        res = (
            await c.call_tool("retrieve_graph_context", {"query": "What is the airspeed of an unladen swallow?"})
        ).structured_content
        rec(
            "unanswerable_abstains",
            res["sufficient_evidence"] is False and not res["snippets"],
            request_id=res["meta"]["request_id"],
        )
        for name, args, code in [
            ("malformed_input", {"entity_id": "x'; DROP"}, "INVALID_ARGUMENT"),
            ("unknown_entity", {"entity_id": "svc-nope"}, "NOT_FOUND"),
        ]:
            try:
                await c.call_tool("get_entity", args)
                rec(name, False, note="no error raised")
            except ToolError as e:
                body = json.loads(str(e))
                rec(
                    name,
                    body["code"] == code and bool(body["request_id"]),
                    code=body["code"],
                    request_id=body["request_id"],
                )
    return steps


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--label", default="deployment")
    ap.add_argument("--token-env", default="GRAPHRAG_SMOKE_TOKEN")
    ap.add_argument("--out")
    a = ap.parse_args()
    steps = asyncio.run(run(a.url, os.environ[a.token_env]))
    report = {
        "target": a.label,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "passed": all(s["pass"] for s in steps),
        "steps": steps,
    }
    text = json.dumps(report, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
