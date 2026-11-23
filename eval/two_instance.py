"""Two-process statelessness demonstration (no sticky sessions).

Starts TWO independent server processes against the same read-only database, puts a round-robin reverse proxy
in front that alternates *every HTTP request* between them, then:
  1. drives the raw 2026-07-28 wire (server/discover, tools/list, tools/call) alternating instances explicitly,
  2. drives a real MCP client (FastMCP Client, negotiates 2026-07-28) through the proxy,
  3. drives a legacy-handshake client (SDK ClientSession.initialize) through the proxy (compatibility check),
  4. kills instance A mid-run and shows the client continues on B with no re-handshake.
It counts per-instance `tool_call` log lines to prove both processes served requests, and checks that no response
ever carries an `Mcp-Session-Id` header. Run: python -m eval.two_instance --out eval/results/two_instance_stateless.json
Needs GRAPHRAG_NEO4J_* (read-only) + GRAPHRAG_AUTH_PUBLIC_KEY(_FILE) env and a token in GRAPHRAG_TOKEN.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastmcp import Client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "raw-wire-client", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
HOP = {"host", "content-length", "connection", "transfer-encoding", "keep-alive"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Instance:
    def __init__(self, name: str, env: dict[str, str], logdir: Path) -> None:
        self.name, self.port = name, free_port()
        self.log = logdir / f"{name}.log"
        e = dict(env, GRAPHRAG_PORT=str(self.port), GRAPHRAG_HOST="127.0.0.1", GRAPHRAG_LOG_LEVEL="INFO")
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "graphrag_mcp.main"], env=e, stdout=self.log.open("w"), stderr=subprocess.STDOUT
        )
        self.url = f"http://127.0.0.1:{self.port}"

    def wait_ready(self) -> None:
        for _ in range(100):
            try:
                if httpx.get(self.url + "/ready", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"{self.name} did not become ready")

    def tool_calls(self) -> int:
        n = 0
        for line in self.log.read_text().splitlines():
            if line.startswith("{") and '"tool_call"' in line:
                n += 1
        return n

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class RoundRobinProxy:
    """Alternates every request between backends; skips a backend that refuses connections."""

    def __init__(self, backends: list[Instance]) -> None:
        self.backends, self.i = backends, 0
        self.seen_session_headers = 0
        self.forwarded = 0
        self.client = httpx.AsyncClient(timeout=30)
        self.port = free_port()
        app = Starlette(routes=[Route("/{path:path}", self.handle, methods=["GET", "POST", "DELETE"])])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        last: Exception | None = None
        for _ in range(len(self.backends)):
            b = self.backends[self.i % len(self.backends)]
            self.i += 1
            try:
                r = await self.client.request(request.method, b.url + request.url.path, content=body, headers=headers)
            except httpx.ConnectError as e:
                last = e
                continue
            self.forwarded += 1
            if any(k.lower() == "mcp-session-id" for k in r.headers):
                self.seen_session_headers += 1
            out = {k: v for k, v in r.headers.items() if k.lower() not in HOP | {"content-encoding"}}
            return Response(r.content, status_code=r.status_code, headers=out)
        return Response(f"no backend: {last}", status_code=502)

    def start(self) -> str:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return f"http://127.0.0.1:{self.port}"
            time.sleep(0.1)
        raise RuntimeError("proxy did not start")

    def stop(self) -> None:
        self.server.should_exit = True


def raw_post(base: str, token: str, method: str, params: dict[str, Any], name: str | None = None) -> httpx.Response:
    h = {
        "authorization": f"Bearer {token}",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": "2026-07-28",
        "mcp-method": method,
    }
    if name:
        h["mcp-name"] = name
    return httpx.post(
        base + "/mcp",
        headers=h,
        timeout=15,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": META}},
    )


async def scenario(env: dict[str, str], token: str) -> dict[str, Any]:
    logdir = Path(tempfile.mkdtemp(prefix="graphrag-two-"))
    a, b = Instance("instance-A", env, logdir), Instance("instance-B", env, logdir)
    proxy = RoundRobinProxy([a, b])
    report: dict[str, Any] = {"pids": {"A": a.proc.pid, "B": b.proc.pid}, "ports": {"A": a.port, "B": b.port}}
    try:
        a.wait_ready()
        b.wait_ready()
        purl = proxy.start()
        # 1. raw wire, explicit alternation: every request is a separate, self-contained call.
        results, session_headers = [], 0
        base0 = {"A": a.tool_calls(), "B": b.tool_calls()}
        for i in range(12):
            inst = a if i % 2 == 0 else b
            seq = [
                ("server/discover", {}, None),
                ("tools/list", {}, None),
                (
                    "tools/call",
                    {
                        "name": "find_relationship_paths",
                        "arguments": {
                            "source_entity_id": "feat-gift-cards",
                            "target_entity_id": "svc-ledger-service",
                            "max_hops": 3,
                            "max_paths": 1,
                        },
                    },
                    "find_relationship_paths",
                ),
            ]
            method, params, name = seq[i % 3]
            r = raw_post(inst.url, token, method, params, name)
            session_headers += sum(1 for k in r.headers if k.lower() == "mcp-session-id")
            body = r.json()
            ok = r.status_code == 200 and "result" in body
            results.append({"i": i, "instance": inst.name, "method": method, "status": r.status_code, "ok": ok})
            if method == "tools/call":
                results[-1]["path"] = json.loads(body["result"]["content"][0]["text"])["paths"][0]["node_ids"]
        report["raw_wire_alternating"] = {
            "requests": len(results),
            "all_ok": all(x["ok"] for x in results),
            "mcp_session_id_headers_seen": session_headers,
            "no_initialize_sent": True,
            "identical_call_results": len({tuple(x["path"]) for x in results if "path" in x}) == 1,
            "detail": results,
        }
        # 2. modern client through the round-robin proxy
        errors = 0
        async with Client(purl + "/mcp", auth=token) as c:
            report["client_negotiated_protocol"] = c.protocol_version
            for _ in range(30):
                try:
                    r = await c.call_tool("search_entities", {"query": "ledger", "limit": 2})
                    assert r.structured_content["entities"][0]["id"] == "svc-ledger-service"
                except Exception:  # noqa: BLE001
                    errors += 1
        report["modern_client_via_proxy"] = {"calls": 30, "errors": errors}
        # 3. legacy-handshake client (compat): initialize on one instance, calls land on both
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        legacy_ok = 0
        async with httpx2.AsyncClient(headers={"authorization": f"Bearer {token}"}) as h:
            async with streamable_http_client(purl + "/mcp", http_client=h) as (rs, ws):
                async with ClientSession(rs, ws) as s:
                    await s.initialize()
                    for _ in range(10):
                        res = await s.call_tool("get_entity", {"entity_id": "svc-ledger-service", "neighbor_limit": 1})
                        legacy_ok += 0 if res.is_error else 1
                    report["legacy_handshake_client_via_proxy"] = {
                        "negotiated": s.protocol_version,
                        "calls_ok": legacy_ok,
                        "calls": 10,
                    }
        report["tool_calls_served"] = {"A": a.tool_calls() - base0["A"], "B": b.tool_calls() - base0["B"]}
        # 4. kill A; the same long-lived client keeps working against B alone
        async with Client(purl + "/mcp", auth=token) as c2:
            await c2.call_tool("search_entities", {"query": "ledger"})
            before_b = b.tool_calls()
            a.stop()
            post_errors = 0
            for _ in range(10):
                try:
                    await c2.call_tool("search_entities", {"query": "gateway"})
                except Exception:  # noqa: BLE001
                    post_errors += 1
            report["after_instance_A_killed"] = {
                "calls": 10,
                "errors": post_errors,
                "extra_calls_served_by_B": b.tool_calls() - before_b,
            }
        report["proxy"] = {
            "requests_forwarded": proxy.forwarded,
            "responses_with_session_id_header": proxy.seen_session_headers,
        }
        t = report["tool_calls_served"]
        report["verdict"] = bool(
            report["raw_wire_alternating"]["all_ok"]
            and report["raw_wire_alternating"]["mcp_session_id_headers_seen"] == 0
            and errors == 0
            and legacy_ok == 10
            and t["A"] > 0
            and t["B"] > 0
            and proxy.seen_session_headers == 0
            and report["after_instance_A_killed"]["errors"] == 0
        )
        return report
    finally:
        proxy.stop()
        a.stop()
        b.stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    env = dict(os.environ)
    rep = asyncio.run(scenario(env, os.environ["GRAPHRAG_TOKEN"]))
    rep["run_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    Path(args.out).write_text(json.dumps(rep, indent=2) + "\n")
    print(json.dumps({k: v for k, v in rep.items() if k != "raw_wire_alternating"}, indent=2))
    raise SystemExit(0 if rep["verdict"] else 1)


if __name__ == "__main__":
    main()
