"""Pure unit tests: normalisation, allowlists, deterministic data generation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from tools import generate_demo

from graphrag_mcp import repository
from graphrag_mcp.schemas import ENTITY_KINDS, RELATIONSHIP_TYPES
from graphrag_mcp.text import normalize, tokens

ROOT = Path(__file__).resolve().parents[2]


def test_normalize_collapses_case_and_whitespace():
    assert normalize("  Ledger \t SERVICE\n") == "ledger service"
    assert normalize("ＬＥＤＧＥＲ") == "ledger"  # NFKC


def test_tokens_drop_stopwords_and_dedupe():
    assert tokens("How is the Ledger related to the ledger?") == ["ledger"]


def test_generation_is_deterministic(tmp_path):
    m1 = generate_demo.write(tmp_path / "a")
    m2 = generate_demo.write(tmp_path / "b")
    assert m1 == m2


def test_committed_dataset_matches_generator(tmp_path):
    fresh = generate_demo.write(tmp_path)
    committed = json.loads((ROOT / "data/demo/manifest.json").read_text())
    assert fresh == committed, "data/demo is stale: rerun `python -m tools.generate_demo`"
    for name, digest in committed["sha256"].items():
        assert hashlib.sha256((ROOT / f"data/demo/{name}.jsonl").read_bytes()).hexdigest() == digest


def test_dataset_invariants():
    d = generate_demo.generate()
    ids = {n["id"] for n in d["nodes"]}
    assert len(ids) == len(d["nodes"])
    assert {n["kind"] for n in d["nodes"]} <= set(ENTITY_KINDS)
    chunk_ids = {c["id"] for c in d["chunks"]}
    for e in d["edges"]:
        assert e["type"] in RELATIONSHIP_TYPES
        assert e["source"] in ids and e["target"] in ids
        assert e["chunk_id"] in chunk_ids
        assert e["statement"] in next(c for c in d["chunks"] if c["id"] == e["chunk_id"])["text"]
    # Synthetic-only guard: every source is explicitly licensed as synthetic.
    assert all("synthetic" in s["license"] for s in d["sources"])


def test_island_is_disconnected():
    d = generate_demo.generate()
    adj: dict[str, set[str]] = {}
    for e in d["edges"]:
        adj.setdefault(e["source"], set()).add(e["target"])
        adj.setdefault(e["target"], set()).add(e["source"])
    seen, todo = set(), ["svc-legacy-archive-tool"]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo.extend(adj.get(n, ()))
    assert seen == {"svc-legacy-archive-tool", "db-cold-archive-tape-store", "team-archive-maintenance-team"}


def test_cypher_relationship_union_comes_only_from_allowlist():
    q = repository._paths_query(3)
    union = re.search(r"\[:([A-Z_|]+)\*1\.\.3\]", q)
    assert union and set(union.group(1).split("|")) == set(RELATIONSHIP_TYPES)
    for h in (0, 5, -1, 99):
        try:
            repository._paths_query(h)
        except ValueError:
            continue
        raise AssertionError(f"hop bound {h} must be rejected")
