"""GraphToolService: validated, bounded, deterministic graph operations.

No MCP, HTTP or Neo4j-driver imports here: the repository is injected, so the whole
module is unit-testable and the transport layer stays thin.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from .config import Settings
from .errors import ToolFailure, invalid
from .observability import db_time_var, get_logger, hash_client, new_request_id
from .repository import Fact, GraphRepository
from .schemas import (
    ENTITY_KINDS,
    MAX_CONTEXT_QUERY_CHARS,
    MAX_ID_CHARS,
    MAX_QUERY_CHARS,
    ContextSnippet,
    ErrorCode,
    FindPathsResult,
    GetEntityResult,
    GraphPath,
    RetrieveContextResult,
    SearchEntitiesResult,
    ToolMeta,
)
from .text import normalize, tokens

log = get_logger("service")
T = TypeVar("T", bound=BaseModel)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_KIND_BY_FOLD = {k.casefold(): k for k in ENTITY_KINDS}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PATHS_PAIR_LIMIT = 3
FACTS_PER_ENTITY = 8


class RateLimiter:
    """Per-principal sliding window (in-memory, per instance; documented limitation)."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, principal: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self._hits[principal]
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= self._per_minute:
            return False
        q.append(now)
        return True


def _clean_text(value: str, *, name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise invalid(f"{name} must be a string")
    if _CTRL.search(value):
        raise invalid(f"{name} contains control characters")
    norm = normalize(value)
    if not norm:
        raise invalid(f"{name} must not be empty")
    if len(norm) > max_chars:
        raise invalid(f"{name} exceeds {max_chars} characters")
    return norm


def _clean_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID_CHARS or not _ID_RE.match(value):
        raise invalid(f"{name} is not a valid entity id")
    return value


def _bounded_int(value: int, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise invalid(f"{name} must be an integer in [{lo}, {hi}]")
    return value


class GraphToolService:
    def __init__(self, repo: GraphRepository, settings: Settings) -> None:
        self._repo = repo
        self._s = settings
        self._limiter = RateLimiter(settings.rate_limit_per_minute)
        self._sem = asyncio.Semaphore(settings.max_concurrency)

    # --- instrumentation -------------------------------------------------------------------
    async def _run(
        self,
        tool: str,
        principal: str,
        work: Callable[[str], Awaitable[tuple[T, int]]],
    ) -> T:
        """Common wrapper: request id, rate limit, concurrency cap, deadline, logging."""
        rid = new_request_id()
        acc = [0.0]
        db_time_var.set(acc)
        t0 = time.perf_counter()
        error: str | None = None
        count = 0
        truncated = False
        try:
            if not self._limiter.check(principal):
                raise ToolFailure(ErrorCode.RATE_LIMITED, "rate limit exceeded; retry later", retryable=True)
            try:
                await asyncio.wait_for(self._sem.acquire(), timeout=1.0)
            except TimeoutError as e:
                raise ToolFailure(ErrorCode.RATE_LIMITED, "server is at its concurrency limit", retryable=True) from e
            try:
                async with asyncio.timeout(self._s.request_deadline_s):
                    result, count = await work(rid)
            except TimeoutError as e:
                raise ToolFailure(ErrorCode.TIMEOUT, "request exceeded its time budget", retryable=True) from e
            finally:
                self._sem.release()
            result = self._bound(result)
            result.meta.latency_ms = round((time.perf_counter() - t0) * 1000, 2)  # type: ignore[attr-defined]
            truncated = result.meta.truncated  # type: ignore[attr-defined]
            return result
        except ToolFailure as f:
            error = f.code.value
            f.request_id = rid
            raise
        except Exception as e:
            error = ErrorCode.INTERNAL_ERROR.value
            log.error("unhandled", tool_name=tool, error_type=type(e).__name__)
            wrapped = ToolFailure(ErrorCode.INTERNAL_ERROR, "internal error")
            wrapped.request_id = rid
            raise wrapped from e
        finally:
            log.info(
                "tool_call",
                tool_name=tool,
                client_id_hash=hash_client(principal),
                success=error is None,
                error_code=error,
                total_latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                db_latency_ms=round(acc[0], 2),
                result_count=count,
                truncated=truncated,
                service_version=self._s.service_version,
                git_sha=self._s.git_sha,
                cloud_revision=self._s.cloud_revision or None,
                graph_revision=self._repo_revision_hint,
            )

    _repo_revision_hint: str = "unknown"

    async def _meta(self, rid: str, truncated: bool = False) -> ToolMeta:
        rev = await self._repo.graph_revision()
        self._repo_revision_hint = rev
        return ToolMeta(request_id=rid, graph_revision=rev, truncated=truncated)

    def _bound(self, result: T) -> T:
        """Shrink list fields until the serialised response fits ``max_response_bytes``."""
        limit = self._s.max_response_bytes
        if len(result.model_dump_json()) <= limit:
            return result
        data: Any = result
        for field in ("snippets", "paths", "neighbors", "entities", "linked_entities"):
            items = getattr(data, field, None)
            while isinstance(items, list) and items and len(data.model_dump_json()) > limit:
                items.pop()
                data.meta.truncated = True
        data.meta.truncated = True
        return result

    # --- tools -------------------------------------------------------------------------------
    async def search_entities(
        self, principal: str, query: str, labels: list[str] | None = None, limit: int = 5
    ) -> SearchEntitiesResult:
        async def work(rid: str) -> tuple[SearchEntitiesResult, int]:
            q = _clean_text(query, name="query", max_chars=MAX_QUERY_CHARS)
            n = _bounded_int(limit, "limit", 1, 10)
            kinds: list[str] | None = None
            if labels:
                kinds = []
                for lab in labels:
                    k = _KIND_BY_FOLD.get(str(lab).strip().casefold())
                    if k is None:
                        raise invalid(f"unknown label; allowed: {', '.join(ENTITY_KINDS)}")
                    kinds.append(k)
                kinds = sorted(set(kinds))
            entities = await self._repo.search_entities(q, kinds, n)
            return SearchEntitiesResult(entities=entities, meta=await self._meta(rid)), len(entities)

        return await self._run("search_entities", principal, work)

    async def get_entity(self, principal: str, entity_id: str, neighbor_limit: int = 10) -> GetEntityResult:
        async def work(rid: str) -> tuple[GetEntityResult, int]:
            eid = _clean_id(entity_id, "entity_id")
            n = _bounded_int(neighbor_limit, "neighbor_limit", 0, 20)
            found = await self._repo.get_entity(eid, n)
            if found is None:
                raise ToolFailure(ErrorCode.NOT_FOUND, f"entity {eid!r} not found")
            entity, neighbors, total = found
            return (
                GetEntityResult(
                    entity=entity,
                    neighbors=neighbors,
                    total_neighbors=total,
                    meta=await self._meta(rid, truncated=total > len(neighbors)),
                ),
                len(neighbors),
            )

        return await self._run("get_entity", principal, work)

    async def find_relationship_paths(
        self, principal: str, source_entity_id: str, target_entity_id: str, max_hops: int = 3, max_paths: int = 5
    ) -> FindPathsResult:
        async def work(rid: str) -> tuple[FindPathsResult, int]:
            src = _clean_id(source_entity_id, "source_entity_id")
            dst = _clean_id(target_entity_id, "target_entity_id")
            hops = _bounded_int(max_hops, "max_hops", 1, 4)
            npaths = _bounded_int(max_paths, "max_paths", 1, 10)
            if src == dst:
                raise invalid("source_entity_id and target_entity_id must differ")
            have = await self._repo.existing_ids([src, dst])
            for label, i in (("source_entity_id", src), ("target_entity_id", dst)):
                if i not in have:
                    raise ToolFailure(ErrorCode.NOT_FOUND, f"{label} {i!r} not found")
            paths = await self._repo.find_paths(src, dst, hops, npaths)
            return FindPathsResult(paths=paths, meta=await self._meta(rid, truncated=len(paths) == npaths)), len(paths)

        return await self._run("find_relationship_paths", principal, work)

    async def retrieve_graph_context(
        self, principal: str, query: str, top_k: int = 5, max_chars: int = 6000
    ) -> RetrieveContextResult:
        async def work(rid: str) -> tuple[RetrieveContextResult, int]:
            _clean_text(query, name="query", max_chars=MAX_CONTEXT_QUERY_CHARS)
            k = _bounded_int(top_k, "top_k", 1, 10)
            budget = _bounded_int(max_chars, "max_chars", 500, 12000)
            qpad = " " + " ".join(re.findall(r"[a-z0-9]+", normalize(query))) + " "
            linked = await self._repo.link_entities(qpad, tokens(query), k)
            if not linked:
                return (
                    RetrieveContextResult(
                        snippets=[],
                        linked_entities=[],
                        paths=[],
                        sufficient_evidence=False,
                        meta=await self._meta(rid),
                    ),
                    0,
                )
            ids = [e.id for e in linked]
            facts = await self._repo.facts(ids, FACTS_PER_ENTITY)
            pair_ids = ids[:PATHS_PAIR_LIMIT]
            pairs = [(a, b) for i, a in enumerate(pair_ids) for b in pair_ids[i + 1 :]]
            paths: list[GraphPath] = []
            if pairs:
                found = await asyncio.gather(*(self._repo.find_paths(a, b, 3, 1) for a, b in pairs))
                paths = [p for group in found for p in group]
            snippets, cut = self._pack(facts, ids, budget)
            capped = any(sum(1 for f in facts if f.entity_id == i) >= FACTS_PER_ENTITY for i in ids)
            return (
                RetrieveContextResult(
                    snippets=snippets,
                    linked_entities=linked,
                    paths=paths,
                    sufficient_evidence=bool(snippets),
                    meta=await self._meta(rid, truncated=cut or capped),
                ),
                len(snippets),
            )

        return await self._run("retrieve_graph_context", principal, work)

    @staticmethod
    def _pack(facts: list[Fact], linked_ids: list[str], budget: int) -> tuple[list[ContextSnippet], bool]:
        rank = {e: i for i, e in enumerate(linked_ids)}
        linked = set(linked_ids)
        seen: set[str] = set()
        ordered: list[Fact] = []
        for f in sorted(
            facts,
            key=lambda f: (
                0 if (f.source_node_id in linked and f.target_node_id in linked) else 1,
                rank.get(f.entity_id, 99),
                f.relationship_type,
                f.relationship_id,
            ),
        ):
            if f.relationship_id not in seen:
                seen.add(f.relationship_id)
                ordered.append(f)
        out: list[ContextSnippet] = []
        used = 0
        for f in ordered:
            text = f.statement or f"{f.source_node_id} {f.relationship_type} {f.target_node_id}"
            if used + len(text) > budget:
                return out, True
            used += len(text)
            out.append(
                ContextSnippet(
                    text=text,
                    entity_ids=[f.source_node_id, f.target_node_id],
                    relationship_id=f.relationship_id,
                    evidence=[f.evidence] if f.evidence else [],
                )
            )
        return out, False
