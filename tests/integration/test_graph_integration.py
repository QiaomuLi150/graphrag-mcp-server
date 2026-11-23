"""Integration tests against a live, disposable Neo4j database (read-only runtime principal)."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import neo4j
import pytest
from tools import seed_demo

from graphrag_mcp.config import Settings
from graphrag_mcp.errors import ToolFailure
from graphrag_mcp.repository import Neo4jRepository
from graphrag_mcp.schemas import ErrorCode
from graphrag_mcp.service import GraphToolService
from tests.conftest import Keys, Neo4jTestDb, make_settings

pytestmark = pytest.mark.neo4j
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
async def repo(settings: Settings):
    r = Neo4jRepository(settings)
    await r.connect()
    yield r
    await r.close()


@pytest.fixture(scope="module")
async def svc(repo, settings):
    return GraphToolService(repo, settings.model_copy(update={"rate_limit_per_minute": 100000}))


@pytest.fixture(scope="module")
def data():
    d = seed_demo.load_dataset(ROOT / "data" / "demo")
    return d


def bfs_shortest(edges, src, dst, max_hops):
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e["source"], set()).add(e["target"])
        adj.setdefault(e["target"], set()).add(e["source"])
    dist = {src: 0}
    q = deque([src])
    while q:
        n = q.popleft()
        for m in adj.get(n, ()):
            if m not in dist:
                dist[m] = dist[n] + 1
                q.append(m)
    d = dist.get(dst)
    return d if d is not None and d <= max_hops else None


# --- search_entities ------------------------------------------------------------------------
async def test_search_exact_then_prefix_then_substring_order(svc):
    r = await svc.search_entities("t", "ledger", limit=10)
    names = [e.name for e in r.entities]
    assert names[0] == "Ledger Service"  # alias "Ledger" is an exact match -> rank 0
    assert all("ledger" in (e.name + " " + " ".join(e.aliases)).casefold() for e in r.entities)
    assert r.entities == (await svc.search_entities("t", "ledger", limit=10)).entities  # deterministic


async def test_search_label_filter_and_case_insensitivity(svc):
    r = await svc.search_entities("t", "LEDGER", labels=["datastore"], limit=10)
    assert r.entities and all(e.labels == ["Datastore"] for e in r.entities)


async def test_search_alias_and_ambiguity(svc):
    r = await svc.search_entities("t", "gateway", limit=10)
    exact = [e.id for e in r.entities if "Gateway" in e.aliases]
    assert set(exact) == {"svc-edge-gateway-service", "svc-partner-gateway-service"}


async def test_search_no_match_returns_empty(svc):
    assert (await svc.search_entities("t", "zzzz-nonexistent-entity")).entities == []


async def test_search_limit_is_respected_and_evidence_attached(svc):
    r = await svc.search_entities("t", "service", limit=3)
    assert len(r.entities) == 3
    assert all(e.evidence and e.evidence[0].source_uri.startswith("demo://") for e in r.entities)


# --- get_entity ------------------------------------------------------------------------------
async def test_get_entity_neighbors_are_bounded_ordered_and_attributed(svc, data):
    r = await svc.get_entity("t", "svc-ledger-service", neighbor_limit=5)
    assert r.entity.id == "svc-ledger-service" and len(r.neighbors) == 5
    expected_total = sum(1 for e in data["edges"] if "svc-ledger-service" in (e["source"], e["target"]))
    assert r.total_neighbors == expected_total and r.meta.truncated is True
    assert all(n.statement and n.evidence and n.evidence[0].chunk_id for n in r.neighbors)
    assert r.neighbors == (await svc.get_entity("t", "svc-ledger-service", neighbor_limit=5)).neighbors


async def test_get_entity_zero_neighbors_and_not_found(svc):
    r = await svc.get_entity("t", "svc-ledger-service", neighbor_limit=0)
    assert r.neighbors == [] and r.total_neighbors > 0
    with pytest.raises(ToolFailure) as e:
        await svc.get_entity("t", "svc-nope")
    assert e.value.code == ErrorCode.NOT_FOUND


# --- find_relationship_paths -----------------------------------------------------------------
async def test_paths_match_independent_bfs_on_source_data(svc, data):
    pairs = [
        ("feat-gift-cards", "svc-ledger-service"),
        ("team-payments-engineering-team", "lib-retry-toolkit"),
        ("prod-harbor-checkout", "db-ledger-key-value-store"),
        ("svc-edge-gateway-service", "q-order-events-queue"),
    ]
    for a, b in pairs:
        want = bfs_shortest(data["edges"], a, b, 4)
        r = await svc.find_relationship_paths("t", a, b, max_hops=4, max_paths=10)
        if want is None:
            assert r.paths == []
            continue
        assert r.paths, f"expected a path {a}->{b}"
        assert len(r.paths[0].relationship_ids) == want  # shortest first, same length as BFS
        # every returned path is a real, node-simple path made of real edges, within bounds
        by_id = {e["id"]: e for e in data["edges"]}
        for p in r.paths:
            assert p.node_ids[0] == a and p.node_ids[-1] == b and len(set(p.node_ids)) == len(p.node_ids)
            assert 1 <= len(p.relationship_ids) <= 4
            for i, rid in enumerate(p.relationship_ids):
                e = by_id[rid]
                ends = (e["source"], e["target"]) if p.directions[i] == "out" else (e["target"], e["source"])
                assert ends == (p.node_ids[i], p.node_ids[i + 1]) and e["type"] == p.relationship_types[i]
            assert p.evidence and all(ev.chunk_id for ev in p.evidence)
        lengths = [len(p.relationship_ids) for p in r.paths]
        assert lengths == sorted(lengths)


async def test_hop_limit_is_enforced(svc, data):
    a, b = "feat-gift-cards", "svc-ledger-service"
    d = bfs_shortest(data["edges"], a, b, 4)
    assert d and d >= 2
    assert (await svc.find_relationship_paths("t", a, b, max_hops=d - 1)).paths == []
    assert (await svc.find_relationship_paths("t", a, b, max_hops=d)).paths


async def test_max_paths_and_determinism(svc):
    a, b = "feat-gift-cards", "svc-ledger-service"
    r1 = await svc.find_relationship_paths("t", a, b, max_hops=4, max_paths=2)
    r2 = await svc.find_relationship_paths("t", a, b, max_hops=4, max_paths=2)
    assert len(r1.paths) == 2 and r1.paths == r2.paths and r1.meta.truncated is True


async def test_no_path_between_disconnected_components_is_empty(svc):
    r = await svc.find_relationship_paths("t", "svc-legacy-archive-tool", "svc-ledger-service", max_hops=4)
    assert r.paths == []


async def test_direct_edge_has_one_hop(svc, data):
    e = data["edges"][0]
    r = await svc.find_relationship_paths("t", e["source"], e["target"], max_hops=1, max_paths=10)
    assert any(p.relationship_ids == [e["id"]] for p in r.paths)


# --- retrieve_graph_context ------------------------------------------------------------------
async def test_context_links_entities_and_is_grounded_in_chunk_text(svc, repo, settings, neo4j_db: Neo4jTestDb):
    r = await svc.retrieve_graph_context("t", "How is Gift Cards connected to the Ledger Service?", top_k=5)
    ids = {e.id for e in r.linked_entities}
    assert {"feat-gift-cards", "svc-ledger-service"} <= ids
    assert r.sufficient_evidence and r.snippets and r.paths
    # Groundedness: every snippet's text is literally contained in the cited chunk in the database.
    admin = neo4j.GraphDatabase.driver(neo4j_db.uri, auth=(neo4j_db.admin_user, neo4j_db.admin_password))
    try:
        for s in r.snippets:
            ev = s.evidence[0]
            rows = admin.execute_query(
                "MATCH (c:Chunk {id: $c}) RETURN c.text AS t", {"c": ev.chunk_id}, database_=neo4j_db.database
            ).records
            assert rows and s.text in rows[0]["t"], f"snippet not supported by {ev.chunk_id}"
    finally:
        admin.close()


async def test_context_insufficient_evidence_for_unknown_topic(svc):
    r = await svc.retrieve_graph_context("t", "What is the airspeed velocity of an unladen swallow?")
    assert r.sufficient_evidence is False and r.snippets == [] and r.linked_entities == []


async def test_context_surfaces_ambiguity_and_respects_budget(svc):
    r = await svc.retrieve_graph_context("t", "tell me about the gateway", top_k=5, max_chars=500)
    assert {"svc-edge-gateway-service", "svc-partner-gateway-service"} <= {e.id for e in r.linked_entities}
    assert sum(len(s.text) for s in r.snippets) <= 500


# --- read-only enforcement -------------------------------------------------------------------
@pytest.mark.parametrize(
    "stmt",
    [
        "CREATE (:Hacked {x: 1})",
        "MATCH (e:Entity) SET e.name = 'pwned'",
        "MATCH (e:Entity) DETACH DELETE e",
        "MATCH ()-[r]->() DELETE r",
        "CREATE INDEX evil FOR (e:Entity) ON (e.x)",
        "MERGE (g:GraphMeta {id: 'current'}) SET g.revision = 'pwned'",
    ],
)
async def test_runtime_principal_cannot_write(settings: Settings, neo4j_db: Neo4jTestDb, stmt):
    if neo4j_db.edition != "enterprise":
        pytest.skip(
            "role-based write denial needs Neo4j Enterprise RBAC (Community: see test_read_access_mode_blocks_writes)"
        )
    d = neo4j.AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value())
    )
    try:
        with pytest.raises(neo4j.exceptions.Neo4jError) as e:
            await d.execute_query(stmt, database_=settings.neo4j_database)
        assert "Security" in e.value.code or "Forbidden" in e.value.code
    finally:
        await d.close()


async def test_runtime_principal_cannot_read_other_databases(settings: Settings, neo4j_db: Neo4jTestDb):
    if neo4j_db.edition != "enterprise":
        pytest.skip("multi-database isolation needs Neo4j Enterprise (Community has a single database)")
    d = neo4j.AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value())
    )
    try:
        for db in ("neo4j", "system"):
            with pytest.raises(neo4j.exceptions.Neo4jError):
                await d.execute_query("MATCH (n) RETURN count(n)", database_=db)
    finally:
        await d.close()


async def test_graph_unchanged_after_all_the_above(neo4j_db: Neo4jTestDb, data):
    admin = neo4j.GraphDatabase.driver(neo4j_db.uri, auth=(neo4j_db.admin_user, neo4j_db.admin_password))
    try:
        report = seed_demo.validate(admin, neo4j_db.database, data)
        assert report["graph_revision"] == neo4j_db.revision
    finally:
        admin.close()


# --- injection / hostile input ----------------------------------------------------------------
HOSTILE = [
    "') MATCH (n) DETACH DELETE n //",
    "x' OR 1=1 //",
    "ledger}) RETURN 1 UNION MATCH (n) RETURN n //",
    "$q",
    "{id: 'x'}",
    "\\",
    "'; DROP DATABASE demo; --",
]


@pytest.mark.parametrize("text", HOSTILE)
async def test_hostile_text_is_treated_as_data(svc, text):
    r = await svc.search_entities("t", text)
    assert r.entities == []
    c = await svc.retrieve_graph_context("t", text)
    # Words that genuinely name an entity (e.g. 'ledger') may link; hostile syntax must never matter.
    assert all(e.id.split("-")[0] in {"team", "prod", "svc", "db", "lib", "q", "feat"} for e in c.linked_entities)
    for bad_id in (text,):
        with pytest.raises(ToolFailure) as e:
            await svc.get_entity("t", bad_id)
        assert e.value.code in (ErrorCode.INVALID_ARGUMENT, ErrorCode.NOT_FOUND)


# --- failure modes ---------------------------------------------------------------------------
async def test_unreachable_database_is_sanitised(neo4j_db: Neo4jTestDb, keys: Keys):
    s = make_settings(
        neo4j_db, keys, neo4j_uri="bolt://127.0.0.1:1", neo4j_connect_timeout_s=0.5, neo4j_max_retry_s=0.2
    )
    r = Neo4jRepository(s)
    await r.connect()
    try:
        svc = GraphToolService(r, s)
        with pytest.raises(ToolFailure) as e:
            await svc.search_entities("t", "x")
        assert e.value.code == ErrorCode.DB_UNAVAILABLE and e.value.retryable
        assert "127.0.0.1" not in e.value.message and "bolt" not in e.value.message
        assert await r.ping() is False
    finally:
        await r.close()


async def test_wrong_credentials_surface_as_unavailable_not_auth_detail(neo4j_db: Neo4jTestDb, keys: Keys):
    s = make_settings(neo4j_db, keys, neo4j_password="definitely-wrong")
    r = Neo4jRepository(s)
    await r.connect()
    try:
        with pytest.raises(ToolFailure) as e:
            await GraphToolService(r, s).search_entities("t", "x")
        assert e.value.code == ErrorCode.DB_UNAVAILABLE
        assert "password" not in e.value.message.lower() and "credential" not in e.value.message.lower()
    finally:
        await r.close()


async def test_server_side_deadline_maps_to_timeout(neo4j_db: Neo4jTestDb, keys: Keys):
    s = make_settings(neo4j_db, keys, request_deadline_s=0.0005)
    r = Neo4jRepository(s)
    await r.connect()
    try:
        with pytest.raises(ToolFailure) as e:
            await r.search_entities("ledger", None, 5)
        assert e.value.code == ErrorCode.TIMEOUT and e.value.retryable
    finally:
        await r.close()


async def test_seeding_is_idempotent(neo4j_db: Neo4jTestDb, data):
    admin = neo4j.GraphDatabase.driver(neo4j_db.uri, auth=(neo4j_db.admin_user, neo4j_db.admin_password))
    try:
        before = seed_demo.validate(admin, neo4j_db.database, data)
        again = seed_demo.seed(admin, neo4j_db.database, data)
        assert before == again
    finally:
        admin.close()


def test_seeder_refuses_non_demo_database_and_remote_reset():
    with pytest.raises(seed_demo.SeedError):
        seed_demo.guard_database("bolt://x", "production", allow_non_demo=False)
    seed_demo.guard_database("bolt://x", "demo", allow_non_demo=False)
    with pytest.raises(seed_demo.SeedError):
        seed_demo.guard_reset("neo4j+s://abc.databases.neo4j.io")
    seed_demo.guard_reset("bolt://127.0.0.1:7687")


def test_seeder_rejects_tampered_dataset(tmp_path):
    for f in (ROOT / "data" / "demo").iterdir():
        (tmp_path / f.name).write_bytes(f.read_bytes())
    with (tmp_path / "nodes.jsonl").open("a") as fh:
        fh.write(json.dumps({"id": "x"}) + "\n")
    with pytest.raises(seed_demo.SeedError):
        seed_demo.load_dataset(tmp_path)


async def test_context_prioritises_direct_facts_between_linked_entities(svc):
    """Regression (found by eval case context-0084): the direct fact between two linked entities
    must survive the per-entity fact cap even when one entity has many neighbours."""
    r = await svc.retrieve_graph_context("t", "How is Partner Gateway Service related to Ledger Service?", top_k=5)
    assert "Partner Gateway Service integrates with Ledger Service." in {s.text for s in r.snippets}


@pytest.mark.parametrize(
    "stmt", ["CREATE (:Hacked {x: 1})", "MATCH (e:Entity) SET e.name = 'pwned'", "MATCH (e:Entity) DETACH DELETE e"]
)
async def test_read_access_mode_blocks_writes_on_any_edition(repo, stmt):
    """Edition-independent defence: every repository call runs in READ access mode, which the server enforces."""
    with pytest.raises(ToolFailure):
        await repo._read(stmt)  # type: ignore[arg-type]
    assert await repo.ping()


async def test_graph_counts_unchanged_after_write_attempts(repo):
    rows = await repo._read("MATCH (e:Entity) RETURN count(e) AS n")
    assert rows[0]["n"] == 146
