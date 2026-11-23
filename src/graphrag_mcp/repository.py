"""Neo4j read repository: reviewed, parameterised, read-only Cypher templates only.

Rules enforced here:
  * every caller-controlled value travels as a query *parameter*;
  * the only strings spliced into Cypher are the relationship-type union (built from the
    code-defined ``RELATIONSHIP_TYPES`` allowlist) and one of four literal hop bounds;
  * every call uses ``RoutingControl.READ`` plus a server-side transaction timeout;
  * the connected principal is expected to hold a read-only role (verified by tests).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, LiteralString, Protocol

import neo4j
from neo4j import AsyncGraphDatabase, Query, RoutingControl
from neo4j import exceptions as nx

from .config import Settings
from .errors import ToolFailure
from .observability import db_time_var, get_logger, request_id_var
from .schemas import (
    RELATIONSHIP_TYPES,
    ErrorCode,
    EvidenceRef,
    GraphEntity,
    GraphPath,
    NeighborSummary,
)

log = get_logger("repository")

ENTITY_EVIDENCE_PER_NODE = 3
_REL_UNION = "|".join(RELATIONSHIP_TYPES)  # from the allowlist constant, never from callers

_ENTITY_PROJECTION: LiteralString = (
    "e.id AS id, e.name AS name, e.description AS description, e.aliases AS aliases, "
    "[l IN labels(e) WHERE l <> 'Entity'] AS labels"
)
_EVIDENCE_CALL: LiteralString = (
    "CALL (e) { OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(e) "
    "RETURN c.id AS cid, c.source_id AS sid ORDER BY cid LIMIT $ev_limit } "
)

SEARCH_Q: LiteralString = (
    "MATCH (e:Entity) "
    "WHERE ($labels IS NULL OR any(l IN labels(e) WHERE l IN $labels)) "
    "AND (e.name_norm CONTAINS $q OR any(a IN e.alias_norms WHERE a CONTAINS $q)) "
    "WITH e, CASE "
    "  WHEN e.name_norm = $q OR $q IN e.alias_norms THEN 0 "
    "  WHEN e.name_norm STARTS WITH $q OR any(a IN e.alias_norms WHERE a STARTS WITH $q) THEN 1 "
    "  ELSE 2 END AS rank "
    "ORDER BY rank, e.name_norm, e.id LIMIT $limit "
    + _EVIDENCE_CALL
    + "RETURN "
    + _ENTITY_PROJECTION
    + ", rank, e.name_norm AS name_norm, collect({cid: cid, sid: sid}) AS ev ORDER BY rank, name_norm, id"
)

GET_ENTITY_Q: LiteralString = (
    "MATCH (e:Entity {id: $id}) "
    "CALL (e) { MATCH (e)-[r]-(n:Entity) "
    "  WITH r, n ORDER BY type(r), n.name_norm, r.id LIMIT $nlimit "
    "  RETURN collect({rid: r.id, rtype: type(r), "
    "    dir: CASE WHEN startNode(r) = e THEN 'out' ELSE 'in' END, "
    "    nid: n.id, nname: n.name, nlabels: [l IN labels(n) WHERE l <> 'Entity'], "
    "    stmt: r.statement, cid: r.chunk_id, sid: r.source_id}) AS neighbors } "
    + _EVIDENCE_CALL
    + "RETURN "
    + _ENTITY_PROJECTION
    + ", COUNT { (e)-[]-(:Entity) } AS total, neighbors, collect({cid: cid, sid: sid}) AS ev"
)

EXISTS_Q: LiteralString = "UNWIND $ids AS id OPTIONAL MATCH (e:Entity {id: id}) RETURN id, e IS NOT NULL AS ok"


def _paths_query(hops: int) -> LiteralString:
    if hops not in (1, 2, 3, 4):  # discrete allowlist; never an arbitrary integer in Cypher
        raise ValueError("hops must be 1..4")
    return (
        "MATCH (a:Entity {id: $src}), (b:Entity {id: $dst}) "
        f"MATCH p = (a)-[:{_REL_UNION}*1..{hops}]-(b) "
        "WHERE ALL(n IN nodes(p) WHERE single(m IN nodes(p) WHERE m = n)) "
        "WITH p ORDER BY length(p), [n IN nodes(p) | n.id], [r IN relationships(p) | r.id] "
        "LIMIT $max_paths "
        "RETURN [n IN nodes(p) | n.id] AS nids, [n IN nodes(p) | n.name] AS names, "
        "[r IN relationships(p) | r.id] AS rids, [r IN relationships(p) | type(r)] AS rtypes, "
        "[i IN range(0, length(p) - 1) | CASE WHEN startNode(relationships(p)[i]) = nodes(p)[i] "
        "  THEN 'out' ELSE 'in' END] AS dirs, "
        "[r IN relationships(p) | {cid: r.chunk_id, sid: r.source_id}] AS ev"
    )


_PATH_QUERIES: dict[int, LiteralString] = {h: _paths_query(h) for h in (1, 2, 3, 4)}

LINK_Q: LiteralString = (
    "MATCH (e:Entity) "
    "WITH e, CASE "
    "  WHEN $qpad CONTAINS (' ' + e.name_words + ' ') THEN 0 "
    "  WHEN any(a IN e.alias_words WHERE $qpad CONTAINS (' ' + a + ' ')) THEN 1 "
    "  WHEN size(e.name_tokens) > 0 AND all(t IN e.name_tokens WHERE t IN $tokens) THEN 2 "
    "  ELSE null END AS tier "
    "WHERE tier IS NOT NULL "
    "WITH e, tier ORDER BY tier, size(e.name_words) DESC, e.id LIMIT $limit "
    + _EVIDENCE_CALL
    + "RETURN "
    + _ENTITY_PROJECTION
    + ", tier, size(e.name_words) AS nlen, collect({cid: cid, sid: sid}) AS ev ORDER BY tier, nlen DESC, id"
)

FACTS_Q: LiteralString = (
    "UNWIND $ids AS eid MATCH (e:Entity {id: eid}) "
    "CALL (e) { MATCH (e)-[r]-(n:Entity) "
    "  RETURN r.id AS rid, type(r) AS rtype, r.statement AS stmt, r.chunk_id AS cid, "
    "    r.source_id AS sid, startNode(r).id AS a_id, endNode(r).id AS b_id "
    "  ORDER BY CASE WHEN n.id IN $ids THEN 0 ELSE 1 END, rtype, n.name_norm, rid LIMIT $per } "
    "RETURN eid, rid, rtype, stmt, cid, sid, a_id, b_id"
)

META_Q: LiteralString = "MATCH (m:GraphMeta {id: 'current'}) RETURN m.revision AS revision"
SOURCES_Q: LiteralString = "MATCH (s:Source) RETURN s.id AS id, s.uri AS uri"


@dataclass(frozen=True)
class Fact:
    entity_id: str
    relationship_id: str
    relationship_type: str
    statement: str | None
    source_node_id: str
    target_node_id: str
    evidence: EvidenceRef | None


class GraphRepository(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def ping(self) -> bool: ...
    async def graph_revision(self) -> str: ...
    async def search_entities(self, q: str, labels: list[str] | None, limit: int) -> list[GraphEntity]: ...
    async def get_entity(
        self, entity_id: str, neighbor_limit: int
    ) -> tuple[GraphEntity, list[NeighborSummary], int] | None: ...
    async def existing_ids(self, ids: list[str]) -> set[str]: ...
    async def find_paths(self, src: str, dst: str, max_hops: int, max_paths: int) -> list[GraphPath]: ...
    async def link_entities(self, qpad: str, tokens: list[str], limit: int) -> list[GraphEntity]: ...
    async def facts(self, entity_ids: list[str], per_entity: int) -> list[Fact]: ...


class Neo4jRepository:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._driver: neo4j.AsyncDriver | None = None
        self._rev: str = "unknown"
        self._sources: dict[str, str] = {}
        self._meta_at: float = float("-inf")
        self._meta_lock = asyncio.Lock()

    # --- lifecycle --------------------------------------------------------------------
    async def connect(self) -> None:
        s = self._s
        self._driver = AsyncGraphDatabase.driver(
            s.neo4j_uri,
            auth=(s.neo4j_user, s.neo4j_password.get_secret_value()),
            max_connection_pool_size=s.neo4j_pool_size,
            connection_timeout=s.neo4j_connect_timeout_s,
            max_transaction_retry_time=s.neo4j_max_retry_s,
            notifications_min_severity="OFF",
        )

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None

    # --- execution core -----------------------------------------------------------------
    async def _read(self, query: LiteralString, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._driver is None:
            raise ToolFailure(ErrorCode.DB_UNAVAILABLE, "graph database is not connected", retryable=True)
        s = self._s
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(s.request_deadline_s):
                records, _, _ = await self._driver.execute_query(
                    Query(query, metadata={"request_id": request_id_var.get()}, timeout=s.query_timeout_s),
                    params or {},
                    routing_=RoutingControl.READ,
                    database_=s.neo4j_database,
                )
            return [r.data() for r in records]
        except TimeoutError as e:
            raise ToolFailure(ErrorCode.TIMEOUT, "graph query exceeded its time budget", retryable=True) from e
        except nx.ClientError as e:
            code = getattr(e, "code", "") or ""
            if "TransactionTimedOut" in code:
                raise ToolFailure(ErrorCode.TIMEOUT, "graph query exceeded its time budget", retryable=True) from e
            log.error("neo4j_client_error", neo4j_code=code)
            if "Security" in code:  # e.g. Forbidden: the principal lacks a privilege we rely on
                raise ToolFailure(ErrorCode.DB_UNAVAILABLE, "graph database refused the request") from e
            raise ToolFailure(ErrorCode.INTERNAL_ERROR, "unexpected graph error") from e
        except (
            nx.ServiceUnavailable,
            nx.SessionExpired,
            nx.TransientError,
            nx.DatabaseUnavailable,
            nx.ConnectionAcquisitionTimeoutError,
            nx.AuthError,
            OSError,
        ) as e:
            log.error("neo4j_unavailable", error_type=type(e).__name__)
            raise ToolFailure(ErrorCode.DB_UNAVAILABLE, "graph database is unavailable", retryable=True) from e
        except nx.Neo4jError as e:
            log.error("neo4j_error", neo4j_code=getattr(e, "code", ""))
            raise ToolFailure(ErrorCode.INTERNAL_ERROR, "unexpected graph error") from e
        finally:
            acc = db_time_var.get()
            if acc is not None:
                acc[0] += (time.perf_counter() - t0) * 1000

    async def ping(self) -> bool:
        try:
            rows = await self._read("RETURN 1 AS ok")
            return bool(rows and rows[0]["ok"] == 1)
        except ToolFailure:
            return False

    # --- graph revision + source URIs (cached) ---------------------------------------------
    async def _refresh_meta(self) -> None:
        if time.monotonic() - self._meta_at < self._s.graph_revision_ttl_s:
            return
        async with self._meta_lock:
            if time.monotonic() - self._meta_at < self._s.graph_revision_ttl_s:
                return
            rows = await self._read(META_Q)
            srcs = await self._read(SOURCES_Q)
            self._rev = rows[0]["revision"] if rows else "unseeded"
            self._sources = {r["id"]: r["uri"] for r in srcs}
            self._meta_at = time.monotonic()

    async def graph_revision(self) -> str:
        await self._refresh_meta()
        return self._rev

    def _evidence(self, ev: list[dict[str, Any]] | None) -> list[EvidenceRef]:
        out: list[EvidenceRef] = []
        seen: set[tuple[str, str | None]] = set()
        for item in ev or []:
            sid, cid = item.get("sid"), item.get("cid")
            if not sid or (sid, cid) in seen:
                continue
            seen.add((sid, cid))
            uri = self._sources.get(sid)
            # Only the demo:// locator scheme is ever returned: no file paths, no network URLs.
            out.append(
                EvidenceRef(source_id=sid, chunk_id=cid, source_uri=uri if uri and uri.startswith("demo://") else None)
            )
        return sorted(out, key=lambda r: (r.source_id, r.chunk_id or ""))

    def _entity(self, row: dict[str, Any]) -> GraphEntity:
        return GraphEntity(
            id=row["id"],
            name=row["name"],
            labels=sorted(row["labels"]),
            description=row.get("description"),
            aliases=sorted(row.get("aliases") or []),
            evidence=self._evidence(row.get("ev")),
        )

    # --- operations -------------------------------------------------------------------------
    async def search_entities(self, q: str, labels: list[str] | None, limit: int) -> list[GraphEntity]:
        await self._refresh_meta()
        rows = await self._read(
            SEARCH_Q, {"q": q, "labels": labels, "limit": limit, "ev_limit": ENTITY_EVIDENCE_PER_NODE}
        )
        return [self._entity(r) for r in rows]

    async def get_entity(
        self, entity_id: str, neighbor_limit: int
    ) -> tuple[GraphEntity, list[NeighborSummary], int] | None:
        await self._refresh_meta()
        rows = await self._read(
            GET_ENTITY_Q, {"id": entity_id, "nlimit": neighbor_limit, "ev_limit": ENTITY_EVIDENCE_PER_NODE}
        )
        if not rows:
            return None
        row = rows[0]
        neighbors = [
            NeighborSummary(
                relationship_id=n["rid"],
                relationship_type=n["rtype"],
                direction=n["dir"],
                entity_id=n["nid"],
                entity_name=n["nname"],
                entity_labels=sorted(n["nlabels"]),
                statement=n.get("stmt"),
                evidence=self._evidence([{"cid": n.get("cid"), "sid": n.get("sid")}]),
            )
            for n in sorted(row["neighbors"], key=lambda n: (n["rtype"], n["nname"].casefold(), n["rid"]))
        ]
        return self._entity(row), neighbors, int(row["total"])

    async def existing_ids(self, ids: list[str]) -> set[str]:
        rows = await self._read(EXISTS_Q, {"ids": ids})
        return {r["id"] for r in rows if r["ok"]}

    async def find_paths(self, src: str, dst: str, max_hops: int, max_paths: int) -> list[GraphPath]:
        await self._refresh_meta()
        rows = await self._read(_PATH_QUERIES[max_hops], {"src": src, "dst": dst, "max_paths": max_paths})
        return [
            GraphPath(
                node_ids=r["nids"],
                node_names=r["names"],
                relationship_ids=r["rids"],
                relationship_types=r["rtypes"],
                directions=r["dirs"],
                evidence=self._evidence(r["ev"]),
            )
            for r in rows
        ]

    async def link_entities(self, qpad: str, tokens: list[str], limit: int) -> list[GraphEntity]:
        await self._refresh_meta()
        rows = await self._read(
            LINK_Q, {"qpad": qpad, "tokens": tokens, "limit": limit, "ev_limit": ENTITY_EVIDENCE_PER_NODE}
        )
        return [self._entity(r) for r in rows]

    async def facts(self, entity_ids: list[str], per_entity: int) -> list[Fact]:
        await self._refresh_meta()
        rows = await self._read(FACTS_Q, {"ids": entity_ids, "per": per_entity})
        out = []
        for r in rows:
            ev = self._evidence([{"cid": r["cid"], "sid": r["sid"]}])
            out.append(
                Fact(
                    entity_id=r["eid"],
                    relationship_id=r["rid"],
                    relationship_type=r["rtype"],
                    statement=r["stmt"],
                    source_node_id=r["a_id"],
                    target_node_id=r["b_id"],
                    evidence=ev[0] if ev else None,
                )
            )
        return out
