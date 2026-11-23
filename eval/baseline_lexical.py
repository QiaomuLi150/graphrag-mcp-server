"""Lexical (TF-IDF) chunk-retrieval baseline vs graph context retrieval on the gold 'context' cases.

Matched budget: the baseline returns the top-k chunks whose total characters stay within the same
``max_chars`` the graph tool used (default 6000). Metric: does the returned text contain the gold fact
statement (the direct relationship between the two entities named in the question)?
Also reports mean returned characters, to expose precision/size differences.

Limitation (stated, not hidden): the questions are templated from the same facts the corpus is built
from, so both methods start from favourable inputs; this measures a floor, not real-world quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from collections import Counter
from pathlib import Path

from eval.evaluate import McpBackend, load_gold
from graphrag_mcp.text import tokens

ROOT = Path(__file__).resolve().parents[1]


def build_index():
    chunks = [json.loads(x) for x in (ROOT / "data/demo/chunks.jsonl").read_text().splitlines()]
    docs = [Counter(tokens(c["text"])) for c in chunks]
    df = Counter(t for d in docs for t in d)
    idf = {t: math.log(1 + len(docs) / n) for t, n in df.items()}
    return chunks, docs, idf


def lexical(query: str, chunks, docs, idf, budget: int) -> list[str]:
    q = Counter(tokens(query))
    scored = sorted(
        ((sum(idf.get(t, 0) * q[t] * d[t] for t in q), i) for i, d in enumerate(docs)), key=lambda x: (-x[0], x[1])
    )
    out, used = [], 0
    for s, i in scored:
        if s <= 0 or used + len(chunks[i]["text"]) > budget:
            continue
        out.append(chunks[i]["text"])
        used += len(chunks[i]["text"])
    return out


async def main_async(url: str, token: str) -> dict:
    chunks, docs, idf = build_index()
    cases = [c for c in load_gold() if c["category"] == "context"]
    be = McpBackend(url, token)
    rows = []
    try:
        for c in cases:
            budget = c["args"].get("max_chars", 6000)
            lex = lexical(c["args"]["query"], chunks, docs, idf, budget)
            res, err = await be.call(c["tool"], c["args"])
            graph_text = [s["text"] for s in res["snippets"]] if res else []
            fact = c["expect"]["fact_statement"]
            rows.append(
                {
                    "id": c["id"],
                    "split": c["split"],
                    "lexical_hit": any(fact in t for t in lex),
                    "lexical_chars": sum(map(len, lex)),
                    "lexical_items": len(lex),
                    "graph_hit": any(fact == t for t in graph_text),
                    "graph_chars": sum(map(len, graph_text)),
                    "graph_items": len(graph_text),
                }
            )
    finally:
        await be.close()
    n = len(rows)
    return {
        "n": n,
        "budget_chars": 6000,
        "lexical_hit_rate": round(sum(r["lexical_hit"] for r in rows) / n, 4),
        "graph_hit_rate": round(sum(r["graph_hit"] for r in rows) / n, 4),
        "lexical_mean_chars": round(sum(r["lexical_chars"] for r in rows) / n),
        "graph_mean_chars": round(sum(r["graph_chars"] for r in rows) / n),
        "note": "templated questions built from corpus facts; favourable to both methods; graph additionally returns paths/linked entities that lexical search cannot",
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp-url", required=True)
    ap.add_argument("--token-env", default="GRAPHRAG_EVAL_TOKEN")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rep = asyncio.run(main_async(a.mcp_url, os.environ[a.token_env]))
    Path(a.out).write_text(json.dumps(rep, indent=2) + "\n")
    print({k: v for k, v in rep.items() if k != "rows"})


if __name__ == "__main__":
    main()
