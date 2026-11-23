"""Database-outage drill against the LOCAL compose stack (never run against shared/cloud data).

Stops the local Neo4j container, verifies the server degrades with sanitized typed errors while
staying alive, restarts Neo4j, and measures time until tool calls succeed again.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

NEO4J = os.environ.get("DRILL_NEO4J_CONTAINER", "graphrag-mcp-dev-neo4j-1")
BASE = os.environ.get("DRILL_BASE_URL", "http://127.0.0.1:28080")
TOKEN = os.environ["GRAPHRAG_EVAL_TOKEN"]
CALL = ("search_entities", {"query": "ledger service", "limit": 1})


async def try_call() -> tuple[bool, str]:
    t = time.perf_counter()
    try:
        async with Client(BASE + "/mcp", auth=TOKEN) as c:
            await c.call_tool(*CALL)
        return True, f"ok {1000 * (time.perf_counter() - t):.0f}ms"
    except ToolError as e:
        b = json.loads(str(e))
        return (
            False,
            f"{b['code']} retryable={b['retryable']} msg={b['message']!r} {1000 * (time.perf_counter() - t):.0f}ms",
        )


async def main() -> None:
    log: dict[str, object] = {}
    log["before"] = (await try_call())[1]
    subprocess.run(["docker", "stop", NEO4J], check=True, capture_output=True)  # noqa: S603, S607
    t_down = time.perf_counter()
    async with httpx.AsyncClient(timeout=30) as h:
        log["during_tool_call"] = (await try_call())[1]
        log["during_health"] = (await h.get(BASE + "/health")).status_code
        r = await h.get(BASE + "/ready")
        log["during_ready"] = [r.status_code, r.json()]
    subprocess.run(["docker", "start", NEO4J], check=True, capture_output=True)  # noqa: S603, S607
    t_up = time.perf_counter()
    attempts = 0
    while True:
        attempts += 1
        ok, msg = await try_call()
        if ok:
            break
        await asyncio.sleep(1)
        if time.perf_counter() - t_up > 180:
            raise SystemExit("did not recover within 180s")
    log["recovered_after_s"] = round(time.perf_counter() - t_up, 1)
    log["recovery_attempts"] = attempts
    log["outage_total_s"] = round(time.perf_counter() - t_down, 1)
    print(json.dumps(log, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
