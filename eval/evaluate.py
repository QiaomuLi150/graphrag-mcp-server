"""Correctness / error-handling / latency evaluation over the frozen gold set.

    # via MCP (any deployment):
    python -m eval.evaluate --mcp-url http://127.0.0.1:28080/mcp --token-env TOKEN --out eval/results/local_mcp.json
    # direct service calls (same code path minus the MCP/HTTP layer; needs read-only DB env):
    python -m eval.evaluate --direct --out eval/results/local_direct.json

Reports what was measured, with the configuration that produced it. Dev cases were used while
building the system; frozen/unseen are reported separately and must not be tuned on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_gold(splits: set[str] | None = None) -> list[dict[str, Any]]:
    cases = [json.loads(x) for x in (ROOT / "eval/gold_questions.jsonl").read_text().splitlines()]
    return [c for c in cases if not splits or c["split"] in splits]


# --- backends -----------------------------------------------------------------------------------
class Backend(ABC):
    name: str

    @abstractmethod
    async def call(self, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """Return (structured result, None) or (None, error_code)."""

    async def close(self) -> None:  # noqa: B027
        pass


class McpBackend(Backend):
    name = "mcp"

    def __init__(self, url: str, token: str) -> None:
        from fastmcp import Client

        self._c = Client(url, auth=token)
        self._open = False

    async def _ensure(self) -> None:
        if not self._open:
            await self._c.__aenter__()
            self._open = True

    async def call(self, tool, args):
        from fastmcp.exceptions import ToolError

        await self._ensure()
        try:
            r = await self._c.call_tool(tool, args)
            return r.structured_content, None
        except ToolError as e:
            try:
                return None, json.loads(str(e))["code"]
            except Exception:  # noqa: BLE001
                return None, "UNTYPED_ERROR"

    async def close(self) -> None:
        if self._open:
            await self._c.__aexit__(None, None, None)


class DirectBackend(Backend):
    name = "direct"

    def __init__(self) -> None:
        from graphrag_mcp.config import get_settings
        from graphrag_mcp.errors import ToolFailure
        from graphrag_mcp.repository import Neo4jRepository
        from graphrag_mcp.service import GraphToolService

        s = get_settings().model_copy(update={"rate_limit_per_minute": 10**9})
        self._repo = Neo4jRepository(s)
        self._svc = GraphToolService(self._repo, s)
        self._failure = ToolFailure
        self._ready = False

    async def call(self, tool, args):
        if not self._ready:
            await self._repo.connect()
            self._ready = True
        try:
            r = await getattr(self._svc, tool)("eval", **self._map(tool, args))
            return json.loads(r.model_dump_json()), None
        except self._failure as f:
            return None, f.code.value
        except TypeError:
            return None, "INVALID_ARGUMENT"

    @staticmethod
    def _map(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return args  # service kwargs share the tool's argument names

    async def close(self) -> None:
        await self._repo.close()


# --- scoring ------------------------------------------------------------------------------------
def _prec_rec(returned: list[str], gold: list[str], k: int) -> tuple[float, float]:
    if not returned:
        return (1.0 if not gold else 0.0), (1.0 if not gold else 0.0)
    g = set(gold)
    hit = len([r for r in returned if r in g])
    return hit / len(returned), (hit / min(k, len(g)) if g else 1.0)


def score(case: dict[str, Any], res: dict[str, Any] | None, err: str | None, chunks: dict[str, str]) -> dict[str, Any]:
    e, cat = case["expect"], case["category"]
    out: dict[str, Any] = {"id": case["id"], "category": cat, "split": case["split"]}
    if "error" in e:
        out.update(correct=err == e["error"], got_error=err)
        return out
    if err is not None or res is None:
        out.update(correct=False, got_error=err)
        return out
    if cat in ("entity_lookup", "alias", "ambiguous"):
        ids = [x["id"] for x in res["entities"]]
        k = case["args"].get("limit", 5)
        p, r = _prec_rec(ids, e.get("all_matches", []), k)
        ok = True
        if "top1" in e:
            ok &= bool(ids) and ids[0] == e["top1"]
        if "contains" in e:
            ok &= set(e["contains"]) <= set(ids)
        if "all_matches" in e and len(e["all_matches"]) <= k:
            ok &= set(ids) == set(e["all_matches"])  # complete result when it fits within the limit
        out.update(correct=bool(ok), precision_at_k=p, recall_at_k=r)
    elif cat == "single_hop":
        got = sorted(n["relationship_id"] for n in res["neighbors"])
        ok = res["total_neighbors"] == e["total"] and (
            got == e["neighbor_relationship_ids"]
            if e["total"] <= 20
            else set(got) <= set(e["neighbor_relationship_ids"])
        )
        out.update(correct=bool(ok), attributed=all(n["evidence"] and n["statement"] for n in res["neighbors"]))
    elif cat in ("multi_hop", "no_path"):
        want = e["paths"]
        got = [{"node_ids": p["node_ids"], "relationship_ids": p["relationship_ids"]} for p in res["paths"]]
        recall = (len([p for p in got if p in want]) / len(want)) if want else (1.0 if not got else 0.0)
        out.update(
            correct=got == want, path_recall_at_k=recall, no_path_correct=(not want and not got) if not want else None
        )
        if want and "shortest" in e:
            out["shortest_correct"] = bool(got) and len(got[0]["relationship_ids"]) == e["shortest"]
    elif cat == "context":
        linked = {x["id"] for x in res["linked_entities"]}
        ev_chunks = {ev["chunk_id"] for s in res["snippets"] for ev in s["evidence"]} | {
            ev["chunk_id"] for p in res["paths"] for ev in p["evidence"]
        }
        sup = [
            bool(s["evidence"]) and any(s["text"] in chunks.get(ev["chunk_id"], "") for ev in s["evidence"])
            for s in res["snippets"]
        ]
        direct_fact = any(s["text"] == e["fact_statement"] for s in res["snippets"])
        out.update(
            correct=set(e["linked"]) <= linked
            and res["sufficient_evidence"]
            and e["evidence_chunk"] in ev_chunks
            and direct_fact,
            evidence_precision=(sum(sup) / len(sup)) if sup else 0.0,
            source_attribution_coverage=(sum(1 for s in res["snippets"] if s["evidence"]) / len(res["snippets"]))
            if res["snippets"]
            else 0.0,
        )
    elif cat == "unanswerable":
        out.update(
            correct=res["sufficient_evidence"] is False and not res["snippets"],
            correct_abstention=res["sufficient_evidence"] is False,
        )
    return out


def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(q * len(s)))], 2) if s else float("nan")


def latency_stats(xs: list[float]) -> dict[str, Any]:
    return {
        "n": len(xs),
        "p50_ms": pct(xs, 0.5),
        "p95_ms": pct(xs, 0.95),
        "p99_ms": pct(xs, 0.99),
        "mean_ms": round(statistics.fmean(xs), 2) if xs else None,
    }


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    v = [r[key] for r in rows if r.get(key) is not None]
    return round(sum(v) / len(v), 4) if v else None


def summarise(rows: list[dict[str, Any]], lat: list[float]) -> dict[str, Any]:
    valid = [r for r in rows if r["category"] != "malformed"]
    malformed = [r for r in rows if r["category"] == "malformed"]
    by_cat: dict[str, dict[str, Any]] = {}
    for c in sorted({r["category"] for r in rows}):
        rs = [r for r in rows if r["category"] == c]
        by_cat[c] = {
            "n": len(rs),
            "correct": sum(r["correct"] for r in rs),
            "accuracy": round(sum(r["correct"] for r in rs) / len(rs), 4),
        }
    no_path = [r for r in rows if r["category"] == "no_path" and r.get("no_path_correct") is not None]
    return {
        "n_cases": len(rows),
        "tool_success_rate": round(sum(1 for r in valid if r.get("got_error") is None) / len(valid), 4)
        if valid
        else None,
        "overall_accuracy": round(sum(r["correct"] for r in rows) / len(rows), 4) if rows else None,
        "error_handling_accuracy": round(sum(r["correct"] for r in malformed) / len(malformed), 4)
        if malformed
        else None,
        "entity_precision_at_k": _mean(rows, "precision_at_k"),
        "entity_recall_at_k": _mean(rows, "recall_at_k"),
        "path_recall_at_k": _mean(rows, "path_recall_at_k"),
        "shortest_path_correct": _mean(rows, "shortest_correct"),
        "no_path_correct_rejection_rate": (sum(r["no_path_correct"] for r in no_path) / len(no_path))
        if no_path
        else None,
        "evidence_precision": _mean(rows, "evidence_precision"),
        "source_attribution_coverage": _mean(rows, "source_attribution_coverage"),
        "correct_abstention_rate": _mean(rows, "correct_abstention"),
        "by_category": by_cat,
        "latency": latency_stats(lat),
    }


async def run(backend: Backend, cases: list[dict[str, Any]], warmup: int = 3) -> dict[str, Any]:
    chunks = {
        c["id"]: c["text"] for c in (json.loads(x) for x in (ROOT / "data/demo/chunks.jsonl").read_text().splitlines())
    }
    for c in cases[:warmup]:  # warm connections/caches; excluded from stats
        await backend.call(c["tool"], c["args"])
    rows, lat = [], []
    per_split: dict[str, tuple[list[dict[str, Any]], list[float]]] = {}
    for c in cases:
        t0 = time.perf_counter()
        res, err = await backend.call(c["tool"], c["args"])
        ms = (time.perf_counter() - t0) * 1000
        row = score(c, res, err, chunks)
        rows.append(row)
        lat.append(ms)
        per_split.setdefault(c["split"], ([], []))
        per_split[c["split"]][0].append(row)
        per_split[c["split"]][1].append(ms)
    return {
        "overall": summarise(rows, lat),
        "by_split": {s: summarise(r, lt) for s, (r, lt) in sorted(per_split.items())},
        "failures": [r for r in rows if not r["correct"]],
    }


async def main_async(a: argparse.Namespace) -> dict[str, Any]:
    if a.direct:
        be: Backend = DirectBackend()
    else:
        token = os.environ[a.token_env]
        be = McpBackend(a.mcp_url, token)
    cases = load_gold(set(a.splits.split(",")) if a.splits else None)
    try:
        result = await run(be, cases)
    finally:
        await be.close()
    manifest = json.loads((ROOT / "eval/gold_manifest.json").read_text())
    return {
        "backend": be.name,
        "target": "in-process" if a.direct else (a.label or "mcp-endpoint"),
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gold": {
            "sha256": manifest["sha256"],
            "total": manifest["total"],
            "dataset_revision": manifest["dataset_revision"],
        },
        "splits_run": a.splits or "all",
        "host": {"platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()},
        "note": a.note,
        **result,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp-url")
    ap.add_argument("--token-env", default="GRAPHRAG_EVAL_TOKEN")
    ap.add_argument("--direct", action="store_true")
    ap.add_argument("--splits", default="", help="comma list of dev,frozen,unseen")
    ap.add_argument("--label", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if not a.direct and not a.mcp_url:
        ap.error("--mcp-url or --direct required")
    report = asyncio.run(main_async(a))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    o = report["overall"]
    print(
        json.dumps(
            {
                k: o[k]
                for k in ("n_cases", "tool_success_rate", "overall_accuracy", "error_handling_accuracy", "latency")
            },
            indent=2,
        )
    )
    print("failures:", len(report["failures"]))


if __name__ == "__main__":
    main()
