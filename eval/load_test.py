"""Warm-latency / concurrency benchmark: direct service calls vs the same calls through MCP.

    python -m eval.load_test --mcp-url URL --token-env TOKEN --levels 1,4,8 --requests 200 --out eval/results/load.json

Workload = the valid (non-malformed) gold cases, cycled in a fixed order. Reports client-observed
end-to-end latency, throughput and error rate per backend and concurrency level. Numbers describe
THIS environment only (record host load alongside them); they are not a latency guarantee.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import time
from pathlib import Path
from typing import Any

from eval.evaluate import Backend, DirectBackend, McpBackend, latency_stats, load_gold


def workload() -> list[dict[str, Any]]:
    return [c for c in load_gold() if c["category"] != "malformed"]


async def drive(make_backend, cases: list[dict[str, Any]], concurrency: int, total: int) -> dict[str, Any]:
    backends: list[Backend] = [make_backend() for _ in range(concurrency)]
    for b in backends:  # warm each connection/session before timing
        for c in cases[:5]:
            await b.call(c["tool"], c["args"])
    lat: list[float] = []
    errors = 0
    counter = iter(range(total))

    async def worker(b: Backend) -> None:
        nonlocal errors
        for i in counter:
            c = cases[i % len(cases)]
            t0 = time.perf_counter()
            res, err = await b.call(c["tool"], c["args"])
            lat.append((time.perf_counter() - t0) * 1000)
            if err is not None:
                errors += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(worker(b) for b in backends))
    wall = time.perf_counter() - t0
    for b in backends:
        await b.close()
    return {
        "concurrency": concurrency,
        "requests": total,
        "errors": errors,
        "error_rate": round(errors / total, 4),
        "throughput_rps": round(total / wall, 1),
        "wall_s": round(wall, 2),
        **latency_stats(lat),
    }


async def main_async(a: argparse.Namespace) -> dict[str, Any]:
    cases = workload()
    levels = [int(x) for x in a.levels.split(",")]
    token = os.environ.get(a.token_env, "")
    out: dict[str, Any] = {"direct": [], "mcp": []}
    for lv in levels:
        out["direct"].append(await drive(DirectBackend, cases, lv, a.requests))
        if a.mcp_url:
            out["mcp"].append(await drive(lambda: McpBackend(a.mcp_url, token), cases, lv, a.requests))
    return {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workload": {"cases": len(cases), "requests_per_level": a.requests, "levels": levels},
        "target": a.label or "mcp-endpoint",
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
            "loadavg": os.getloadavg(),
        },
        "note": a.note,
        **out,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp-url", default="")
    ap.add_argument("--token-env", default="GRAPHRAG_EVAL_TOKEN")
    ap.add_argument("--levels", default="1,4,8")
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--label", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rep = asyncio.run(main_async(a))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n")
    for kind in ("direct", "mcp"):
        for r in rep[kind]:
            print(
                kind,
                {
                    k: r[k]
                    for k in ("concurrency", "requests", "error_rate", "throughput_rps", "p50_ms", "p95_ms", "p99_ms")
                },
            )


if __name__ == "__main__":
    main()
