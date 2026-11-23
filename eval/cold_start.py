"""Local container cold-start timing (fresh container of the runtime image on the local Docker host).

NOT a hosted-platform cold start. Requires the compose stack's Neo4j to be running and the runtime image built.
Usage: GRAPHRAG_READER_PASSWORD=... python -m eval.cold_start --network graphrag-mcp-dev_default --image graphrag-mcp-dev-server
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastmcp import Client


async def one(i: int, a: argparse.Namespace, token: str, pub: str) -> dict[str, float]:
    name = f"graphrag-cold-{i}"
    t0 = time.perf_counter()
    env = {
        "GRAPHRAG_NEO4J_URI": "bolt://neo4j:7687",
        "GRAPHRAG_NEO4J_USER": "graphrag_reader",
        "GRAPHRAG_NEO4J_PASSWORD": os.environ["GRAPHRAG_READER_PASSWORD"],
        "GRAPHRAG_NEO4J_DATABASE": "demo",
        "GRAPHRAG_AUTH_ISSUER": "https://dev-issuer.local",
        "GRAPHRAG_AUTH_AUDIENCE": "graphrag-mcp-local",
        "GRAPHRAG_AUTH_PUBLIC_KEY": pub,
    }
    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--network",
        a.network,
        "-p",
        "127.0.0.1:28081:8080",
        "--read-only",
        "--cap-drop",
        "ALL",
    ]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    subprocess.run([*cmd, a.image], check=True, capture_output=True)  # noqa: S603
    try:
        async with httpx.AsyncClient() as h:
            while True:
                try:
                    if (await h.get("http://127.0.0.1:28081/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
        out: dict[str, float] = {"container_start_to_ready_s": round(time.perf_counter() - t0, 2)}
        for label in ("first", "second"):
            t = time.perf_counter()
            async with Client("http://127.0.0.1:28081/mcp", auth=token) as c:
                await c.call_tool("search_entities", {"query": "ledger service", "limit": 3})
            out[f"{label}_client_session_plus_call_ms"] = round((time.perf_counter() - t) * 1000)
        return out
    finally:
        subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True)  # noqa: S603, S607


async def main_async(a: argparse.Namespace) -> None:
    token = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "tools.dev_token", "mint"],
        capture_output=True,
        text=True,
        check=True,  # noqa: S607
    ).stdout.strip()
    pub = Path(".local/dev_jwt_public.pem").read_text()
    runs = [await one(i, a, token, pub) for i in range(a.runs)]
    doc = {"description": "fresh runtime-image container on the local Docker host; NOT a hosted platform", "runs": runs}
    Path(a.out).write_text(json.dumps(doc, indent=2) + "\n")
    print(json.dumps(runs))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="graphrag-mcp-dev_default")
    ap.add_argument("--image", default="graphrag-mcp-dev-server")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--out", default="eval/results/cold_start_local_container.json")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
