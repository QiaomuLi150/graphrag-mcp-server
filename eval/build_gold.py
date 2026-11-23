"""Build the deterministic gold evaluation set from the synthetic demo dataset.

All oracles are computed from ``data/demo/*.jsonl`` with plain Python (BFS/DFS, substring
rules), independently of Neo4j and of the server code. Cases are SYNTHETIC and TEMPLATED;
they are not customer tasks. Split policy: within each category cases are ordered by a
SHA-256 of their id and assigned 40% dev / 30% frozen / 30% unseen, so the split does not
depend on the order in which cases were written.

    python -m eval.build_gold            # writes eval/gold_questions.jsonl + eval/gold_manifest.json
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import deque
from pathlib import Path
from typing import Any

from graphrag_mcp.text import normalize

ROOT = Path(__file__).resolve().parents[1]
SEED = 7771


def load() -> dict[str, list[dict[str, Any]]]:
    return {
        n: [json.loads(x) for x in (ROOT / f"data/demo/{n}.jsonl").read_text().splitlines()]
        for n in ("nodes", "edges", "chunks")
    }


def adjacency(edges):
    adj: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for e in edges:
        adj.setdefault(e["source"], []).append((e["target"], e))
        adj.setdefault(e["target"], []).append((e["source"], e))
    return adj


def bfs_dist(adj, src):
    dist = {src: 0}
    q = deque([src])
    while q:
        n = q.popleft()
        for m, _ in adj.get(n, ()):
            if m not in dist:
                dist[m] = dist[n] + 1
                q.append(m)
    return dist


def expected_paths(adj, src, dst, max_hops, max_paths, names):
    """Exhaustive simple-path enumeration, ordered by (length, node ids, relationship ids)."""
    found: list[tuple[int, list[str], list[str]]] = []

    def dfs(node, nodes, rels):
        if len(rels) > max_hops:
            return
        if node == dst and rels:
            found.append((len(rels), list(nodes), list(rels)))
            return
        if len(rels) == max_hops:
            return
        for nxt, e in adj.get(node, ()):
            if nxt not in nodes:
                nodes.append(nxt)
                rels.append(e["id"])
                dfs(nxt, nodes, rels)
                nodes.pop()
                rels.pop()

    dfs(src, [src], [])
    found.sort(key=lambda t: (t[0], t[1], t[2]))
    return [{"node_ids": n, "relationship_ids": r} for _, n, r in found[:max_paths]]


def build() -> list[dict[str, Any]]:
    d = load()
    rng = random.Random(SEED)  # noqa: S311
    nodes, edges = d["nodes"], d["edges"]
    names = {n["id"]: n["name"] for n in nodes}
    adj = adjacency(edges)
    island = {"svc-legacy-archive-tool", "db-cold-archive-tape-store", "team-archive-maintenance-team"}
    main_nodes = [n for n in nodes if n["id"] not in island]
    cases: list[dict[str, Any]] = []

    def add(cat: str, tool: str, args: dict[str, Any], expect: dict[str, Any]) -> None:
        cid = f"{cat}-{len(cases):04d}"
        cases.append({"id": cid, "category": cat, "tool": tool, "args": args, "expect": expect})

    def norm_hits(q: str) -> list[str]:
        qn = normalize(q)
        return sorted(
            n["id"] for n in nodes if qn in normalize(n["name"]) or any(qn in normalize(a) for a in n["aliases"])
        )

    # 1. entity lookup by exact name (top1 must be the entity)
    for n in rng.sample(main_nodes, 22):
        add(
            "entity_lookup",
            "search_entities",
            {"query": n["name"], "limit": 5},
            {"top1": n["id"], "all_matches": norm_hits(n["name"])},
        )
    # 2. alias lookup
    aliased = [n for n in main_nodes if n["aliases"]]
    for n in rng.sample(aliased, 12):
        a = rng.choice(n["aliases"])
        hits = norm_hits(a)
        add("alias", "search_entities", {"query": a, "limit": 10}, {"contains": [n["id"]], "all_matches": hits})
    # 3. ambiguity: shared alias must surface every candidate
    add(
        "ambiguous",
        "search_entities",
        {"query": "gateway", "limit": 10},
        {"contains": ["svc-edge-gateway-service", "svc-partner-gateway-service"], "all_matches": norm_hits("gateway")},
    )
    add(
        "ambiguous",
        "search_entities",
        {"query": "Gateway", "labels": ["Service"], "limit": 10},
        {
            "contains": ["svc-edge-gateway-service", "svc-partner-gateway-service"],
            "all_matches": [i for i in norm_hits("Gateway") if i.startswith("svc-")],
        },
    )
    for q in ("ledger", "payments", "queue"):
        add("ambiguous", "search_entities", {"query": q, "limit": 10}, {"all_matches": norm_hits(q)})
    # 4. single-hop neighbourhoods
    for n in rng.sample(main_nodes, 14):
        nb = sorted({e["id"] for _, e in adj.get(n["id"], ())})
        add(
            "single_hop",
            "get_entity",
            {"entity_id": n["id"], "neighbor_limit": 20},
            {"total": len(nb), "neighbor_relationship_ids": nb},
        )
    # 5. multi-hop paths with exhaustive-oracle expectations
    ids = [n["id"] for n in main_nodes]
    tries = 0
    want = {2: 8, 3: 8, 4: 6}
    while any(v > 0 for v in want.values()) and tries < 5000:
        tries += 1
        a, b = rng.sample(ids, 2)
        dist = bfs_dist(adj, a).get(b)
        if dist in want and want[dist] > 0:
            want[dist] -= 1
            exp = expected_paths(adj, a, b, 4, 5, names)
            add(
                "multi_hop",
                "find_relationship_paths",
                {"source_entity_id": a, "target_entity_id": b, "max_hops": 4, "max_paths": 5},
                {"shortest": dist, "paths": exp},
            )
    # 6. no-path: disconnected island, and hop-limit shorter than shortest distance
    for i in sorted(island):
        add(
            "no_path",
            "find_relationship_paths",
            {"source_entity_id": i, "target_entity_id": rng.choice(ids), "max_hops": 4},
            {"paths": []},
        )
    cnt = 0
    while cnt < 6:
        a, b = rng.sample(ids, 2)
        dist = bfs_dist(adj, a).get(b)
        if dist and dist >= 3:
            add(
                "no_path",
                "find_relationship_paths",
                {"source_entity_id": a, "target_entity_id": b, "max_hops": dist - 1, "max_paths": 5},
                {"paths": expected_paths(adj, a, b, dist - 1, 5, names)},
            )
            cnt += 1
    # 7. context retrieval for connected pairs (direct facts exist)
    for e in rng.sample(edges, 12):
        if e["source"] in island or e["target"] in island:
            continue
        q = f"How is {names[e['source']]} related to {names[e['target']]}?"
        add(
            "context",
            "retrieve_graph_context",
            {"query": q, "top_k": 5},
            {"linked": [e["source"], e["target"]], "evidence_chunk": e["chunk_id"], "fact_statement": e["statement"]},
        )
    # 8. unanswerable context
    for q in (
        "What is the airspeed velocity of an unladen swallow?",
        "Summarise the 2019 quarterly revenue forecast.",
        "Who won the football world cup?",
        "Explain how photosynthesis works.",
        "zzzz qqqq xxxx",
        "Tell me the CEO's home address.",
    ):
        add("unanswerable", "retrieve_graph_context", {"query": q, "top_k": 5}, {"sufficient": False})
    # 9. malformed / hostile input -> typed errors
    bad = [
        ("get_entity", {"entity_id": "svc-does-not-exist"}, "NOT_FOUND"),
        ("get_entity", {"entity_id": "x'; MATCH (n) DETACH DELETE n //"}, "INVALID_ARGUMENT"),
        ("get_entity", {"entity_id": ""}, "INVALID_ARGUMENT"),
        ("get_entity", {"entity_id": "svc-ledger-service", "neighbor_limit": 500}, "INVALID_ARGUMENT"),
        ("search_entities", {"query": "x" * 300}, "INVALID_ARGUMENT"),
        ("search_entities", {"query": "ok", "limit": 0}, "INVALID_ARGUMENT"),
        ("search_entities", {"query": "ok", "labels": ["Robot"]}, "INVALID_ARGUMENT"),
        (
            "find_relationship_paths",
            {"source_entity_id": "svc-ledger-service", "target_entity_id": "svc-ledger-service"},
            "INVALID_ARGUMENT",
        ),
        (
            "find_relationship_paths",
            {"source_entity_id": "svc-ledger-service", "target_entity_id": "svc-nope"},
            "NOT_FOUND",
        ),
        (
            "find_relationship_paths",
            {"source_entity_id": "svc-ledger-service", "target_entity_id": "feat-gift-cards", "max_hops": 9},
            "INVALID_ARGUMENT",
        ),
        ("retrieve_graph_context", {"query": "ok", "max_chars": 5}, "INVALID_ARGUMENT"),
        ("retrieve_graph_context", {"query": "q" * 900}, "INVALID_ARGUMENT"),
    ]
    for tool, args, code in bad:
        add("malformed", tool, args, {"error": code})
    # split
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for c in cases:
        by_cat.setdefault(c["category"], []).append(c)
    for lst in by_cat.values():
        lst.sort(key=lambda c: hashlib.sha256(c["id"].encode()).hexdigest())
        for i, c in enumerate(lst):
            frac = (i + 0.5) / len(lst)
            c["split"] = "dev" if frac < 0.4 else ("frozen" if frac < 0.7 else "unseen")
    return sorted(cases, key=lambda c: c["id"])


def main() -> None:
    cases = build()
    text = "".join(json.dumps(c, sort_keys=True) + "\n" for c in cases)
    (ROOT / "eval/gold_questions.jsonl").write_text(text)
    counts: dict[str, dict[str, int]] = {}
    for c in cases:
        counts.setdefault(c["category"], {}).setdefault(c["split"], 0)
        counts[c["category"]][c["split"]] += 1
    manifest = {
        "total": len(cases),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "seed": SEED,
        "dataset_revision": json.loads((ROOT / "data/demo/manifest.json").read_text())["graph_revision"],
        "splits": {s: sum(1 for c in cases if c["split"] == s) for s in ("dev", "frozen", "unseen")},
        "by_category": counts,
        "note": "Synthetic templated cases with oracles computed from data/demo; not real user tasks.",
    }
    (ROOT / "eval/gold_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest["splits"]), manifest["total"])


if __name__ == "__main__":
    main()
