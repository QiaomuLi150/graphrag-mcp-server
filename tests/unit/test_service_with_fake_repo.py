"""Service-layer unit tests against an in-memory FAKE repository (a mock: not integration evidence)."""

from __future__ import annotations

import asyncio

import pytest

from graphrag_mcp.config import Settings
from graphrag_mcp.errors import ToolFailure
from graphrag_mcp.repository import Fact
from graphrag_mcp.schemas import ErrorCode, EvidenceRef, GraphEntity, GraphPath
from graphrag_mcp.service import GraphToolService, RateLimiter


def ent(i: str, name: str = "N") -> GraphEntity:
    return GraphEntity(id=i, name=name, labels=["Service"], evidence=[EvidenceRef(source_id="s", chunk_id="c")])


class FakeRepo:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entities = [ent("a"), ent("b")]
        self.fail: ToolFailure | None = None
        self.delay = 0.0

    async def _maybe(self, name: str) -> None:
        self.calls.append(name)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def ping(self) -> bool:
        return True

    async def graph_revision(self) -> str:
        return "rev-1"

    async def search_entities(self, q, labels, limit):
        await self._maybe("search")
        return self.entities[:limit]

    async def get_entity(self, entity_id, neighbor_limit):
        await self._maybe("get")
        return (self.entities[0], [], 0) if entity_id == "a" else None

    async def existing_ids(self, ids):
        await self._maybe("exists")
        return {i for i in ids if i in {"a", "b"}}

    async def find_paths(self, src, dst, max_hops, max_paths):
        await self._maybe("paths")
        return [
            GraphPath(
                node_ids=[src, dst],
                node_names=["A", "B"],
                relationship_ids=["r"],
                relationship_types=["USES"],
                directions=["out"],
            )
        ]

    async def link_entities(self, qpad, tokens, limit):
        await self._maybe("link")
        return [e for e in self.entities if e.id in qpad.split()][:limit]

    async def facts(self, ids, per):
        await self._maybe("facts")
        return [
            Fact("a", f"r{i}", "USES", "x" * 100, "a", "b", EvidenceRef(source_id="s", chunk_id="c")) for i in range(30)
        ]


def make(repo: FakeRepo | None = None, **over) -> tuple[GraphToolService, FakeRepo]:
    repo = repo or FakeRepo()
    s = Settings(auth_issuer="i", auth_audience="a", auth_public_key="k", **over)
    return GraphToolService(repo, s), repo  # type: ignore[arg-type]


async def fails(coro, code: ErrorCode) -> ToolFailure:
    with pytest.raises(ToolFailure) as e:
        await coro
    assert e.value.code == code
    assert e.value.request_id != "-"
    return e.value


async def test_invalid_arguments_never_reach_the_database():
    svc, repo = make()
    await fails(svc.search_entities("p", "   "), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.search_entities("p", "x" * 201), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.search_entities("p", "ok", limit=11), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.search_entities("p", "ok", limit=True), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.search_entities("p", "ok", labels=["Nope"]), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.search_entities("p", "bad\x00char"), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.get_entity("p", "a; MATCH (n) DETACH DELETE n"), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.get_entity("p", "a", neighbor_limit=21), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.find_relationship_paths("p", "a", "b", max_hops=5), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.find_relationship_paths("p", "a", "b", max_paths=0), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.find_relationship_paths("p", "a", "a"), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.retrieve_graph_context("p", "q", max_chars=499), ErrorCode.INVALID_ARGUMENT)
    await fails(svc.retrieve_graph_context("p", "q" * 501), ErrorCode.INVALID_ARGUMENT)
    assert repo.calls == []


async def test_label_matching_is_case_insensitive_and_allowlisted():
    svc, _ = make()
    r = await svc.search_entities("p", "ledger", labels=["service", "TEAM"])
    assert r.entities


async def test_not_found_paths():
    svc, _ = make()
    await fails(svc.get_entity("p", "zzz"), ErrorCode.NOT_FOUND)
    await fails(svc.find_relationship_paths("p", "a", "missing"), ErrorCode.NOT_FOUND)


async def test_no_match_returns_empty_not_invented():
    svc, repo = make()
    repo.entities = []
    assert (await svc.search_entities("p", "nothing")).entities == []
    r = await svc.retrieve_graph_context("p", "nothing relevant at all")
    assert r.sufficient_evidence is False and r.snippets == [] and r.paths == []


async def test_database_failure_is_sanitised_and_typed():
    svc, repo = make()
    repo.fail = ToolFailure(ErrorCode.DB_UNAVAILABLE, "graph database is unavailable", retryable=True)
    f = await fails(svc.search_entities("p", "x"), ErrorCode.DB_UNAVAILABLE)
    assert f.retryable and "bolt" not in f.message


async def test_unexpected_exception_becomes_internal_error_without_details():
    svc, repo = make()

    async def boom(*a, **k):
        raise RuntimeError("password=hunter2 at bolt://secret")

    repo.search_entities = boom  # type: ignore[method-assign]
    f = await fails(svc.search_entities("p", "x"), ErrorCode.INTERNAL_ERROR)
    assert "hunter2" not in f.message and "bolt" not in f.message


async def test_request_deadline_maps_to_timeout():
    svc, repo = make(request_deadline_s=0.05)
    repo.delay = 0.5
    await fails(svc.search_entities("p", "x"), ErrorCode.TIMEOUT)


async def test_rate_limit_per_principal():
    svc, _ = make(rate_limit_per_minute=3)
    for _ in range(3):
        await svc.search_entities("alice", "x")
    await fails(svc.search_entities("alice", "x"), ErrorCode.RATE_LIMITED)
    await svc.search_entities("bob", "x")  # independent budget


def test_rate_limiter_window_slides():
    rl = RateLimiter(2)
    assert rl.check("a", 0) and rl.check("a", 1) and not rl.check("a", 2)
    assert rl.check("a", 61.5)


async def test_concurrency_cap_rejects_instead_of_queueing_forever():
    svc, repo = make(max_concurrency=1, rate_limit_per_minute=1000)
    repo.delay = 1.6
    first = asyncio.create_task(svc.search_entities("p", "x"))
    await asyncio.sleep(0.05)
    await fails(svc.search_entities("p", "x"), ErrorCode.RATE_LIMITED)
    await first


async def test_context_budget_and_truncation_flag():
    svc, repo = make()
    r = await svc.retrieve_graph_context("p", "a b", top_k=2, max_chars=500)
    assert sum(len(s.text) for s in r.snippets) <= 500
    assert r.meta.truncated is True
    assert all(s.evidence for s in r.snippets)
    assert len({s.relationship_id for s in r.snippets}) == len(r.snippets)


async def test_response_size_bound_sets_truncated():
    svc, repo = make(max_response_bytes=800)
    repo.entities = [ent(f"e{i}", "N" * 50) for i in range(10)]
    r = await svc.search_entities("p", "x", limit=10)
    assert len(r.model_dump_json()) <= 800
    assert r.meta.truncated is True and 0 < len(r.entities) < 10


async def test_result_is_stable_across_calls():
    svc, _ = make()
    a = await svc.find_relationship_paths("p", "a", "b")
    b = await svc.find_relationship_paths("p", "a", "b")
    assert a.paths == b.paths
