"""Offline, privileged seeder for the public demo graph.

This module holds the ONLY write path to the graph. It is never imported by the server
and is excluded from the serving container image. It reads *separate* admin credentials
(``GRAPHRAG_SEED_*``) and refuses to touch a database that is not clearly a demo database.

Usage:
    python -m tools.seed_demo --dataset data/demo --create-database --provision-reader

Environment:
    GRAPHRAG_SEED_URI, GRAPHRAG_SEED_USER, GRAPHRAG_SEED_PASSWORD, GRAPHRAG_SEED_DATABASE
    GRAPHRAG_READER_PASSWORD   (only with --provision-reader; the read-only runtime password)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import neo4j

from graphrag_mcp.schemas import ENTITY_KINDS, RELATIONSHIP_TYPES
from graphrag_mcp.text import normalize, tokens

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,62}$")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
BATCH = 500


class SeedError(RuntimeError):
    pass


def _ident(value: str, what: str) -> str:
    if not _IDENT.match(value):
        raise SeedError(f"invalid {what} identifier: {value!r}")
    return value


def _words(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", normalize(s)))


def load_dataset(path: Path) -> dict[str, Any]:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    data: dict[str, list[dict[str, Any]]] = {}
    for name in ("nodes", "edges", "sources", "chunks"):
        raw = (path / f"{name}.jsonl").read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest["sha256"][name]:
            raise SeedError(f"{name}.jsonl does not match manifest checksum; regenerate or fix")
        data[name] = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
        if len(data[name]) != manifest["counts"][name]:
            raise SeedError(f"{name}: row count differs from manifest")
    return {"manifest": manifest, **data}


def guard_database(uri: str, database: str, *, allow_non_demo: bool, community: bool = False) -> None:
    if community and database == "neo4j":  # Community edition has exactly one database
        return
    if not allow_non_demo and not database.startswith(("demo", "gr-test-")):
        raise SeedError(
            f"refusing to seed database {database!r}: only 'demo*' or 'gr-test-*' are allowed "
            "(pass --allow-non-demo-database to override deliberately)"
        )


def guard_reset(uri: str) -> None:
    host = re.sub(r"^[a-z+]+://", "", uri).split(":")[0].split("/")[0]
    if host not in _LOCAL_HOSTS and host != "[::1]":
        raise SeedError("--reset is only permitted against a local disposable database")


def create_database(driver: neo4j.Driver, database: str) -> None:
    _ident(database, "database")
    driver.execute_query(f"CREATE DATABASE `{database}` IF NOT EXISTS WAIT 60 SECONDS", database_="system")


def provision_reader(driver: neo4j.Driver, database: str, user: str, password: str) -> str:
    """Create a read-only role limited to ``database`` and a login user holding it.

    Deliberately NOT the built-in ``reader`` role (which reads every database).
    """
    _ident(database, "database")
    _ident(user, "user")
    role = ("graphrag_ro_" + re.sub(r"[^A-Za-z0-9]", "_", database))[:63]
    stmts = [
        f"CREATE ROLE `{role}` IF NOT EXISTS",
        f"GRANT ACCESS ON DATABASE `{database}` TO `{role}`",
        f"GRANT MATCH {{*}} ON GRAPH `{database}` NODES * TO `{role}`",
        f"GRANT MATCH {{*}} ON GRAPH `{database}` RELATIONSHIPS * TO `{role}`",
    ]
    if database != "neo4j":  # PUBLIC grants access to the default database; close that door
        stmts.append(f"DENY ACCESS ON DATABASE `neo4j` TO `{role}`")
    for s in stmts:
        driver.execute_query(s, database_="system")
    driver.execute_query(
        f"CREATE USER `{user}` IF NOT EXISTS SET PASSWORD $pw CHANGE NOT REQUIRED",
        {"pw": password},
        database_="system",
    )
    driver.execute_query(f"GRANT ROLE `{role}` TO `{user}`", database_="system")
    return role


def provision_community_user(driver: neo4j.Driver, user: str, password: str) -> None:
    """Community edition has no roles/databases: create a separate login for the server (no admin password reuse).

    NOTE: this is NOT a privilege boundary (Community users are not role-restricted). Write protection in this
    mode comes from READ access-mode transactions (server-enforced) and fixed query templates.
    """
    _ident(user, "user")
    driver.execute_query(
        f"CREATE USER `{user}` IF NOT EXISTS SET PASSWORD $pw CHANGE NOT REQUIRED", {"pw": password}, database_="system"
    )


def drop_database(driver: neo4j.Driver, database: str, user: str | None = None, role: str | None = None) -> None:
    """Remove a disposable test database (and optionally its reader user/role)."""
    _ident(database, "database")
    if not database.startswith("gr-test-"):
        raise SeedError("drop is only allowed for gr-test-* databases")
    driver.execute_query(f"DROP DATABASE `{database}` IF EXISTS WAIT", database_="system")
    if user:
        driver.execute_query(f"DROP USER `{_ident(user, 'user')}` IF EXISTS", database_="system")
    if role:
        driver.execute_query(f"DROP ROLE `{_ident(role, 'role')}` IF EXISTS", database_="system")


def _run(driver: neo4j.Driver, database: str, q: Any, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    recs, _, _ = driver.execute_query(q, params or {}, database_=database, routing_=neo4j.RoutingControl.WRITE)
    return [r.data() for r in recs]


def _batches(rows: list[dict[str, Any]]) -> Any:
    for i in range(0, len(rows), BATCH):
        yield rows[i : i + BATCH]


def seed(driver: neo4j.Driver, database: str, dataset: dict[str, Any], *, prune: bool = False) -> dict[str, Any]:
    """Idempotent upsert keyed by stable ids. Returns post-seed validation counts."""
    for stmt in (
        "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
        "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
        "CREATE CONSTRAINT source_id IF NOT EXISTS FOR (s:Source) REQUIRE s.id IS UNIQUE",
        "CREATE CONSTRAINT meta_id IF NOT EXISTS FOR (m:GraphMeta) REQUIRE m.id IS UNIQUE",
        "CREATE INDEX entity_name_norm IF NOT EXISTS FOR (e:Entity) ON (e.name_norm)",
        "CREATE TEXT INDEX entity_name_text IF NOT EXISTS FOR (e:Entity) ON (e.name_norm)",
    ):
        _run(driver, database, stmt)

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for n in dataset["nodes"]:
        if n["kind"] not in ENTITY_KINDS:
            raise SeedError(f"unknown entity kind {n['kind']!r}")
        by_kind.setdefault(n["kind"], []).append(
            {
                "id": n["id"],
                "name": n["name"],
                "name_norm": normalize(n["name"]),
                "name_words": _words(n["name"]),
                "name_tokens": tokens(n["name"]),
                "description": n["description"],
                "aliases": n["aliases"],
                "alias_norms": sorted({normalize(a) for a in n["aliases"]}),
                "alias_words": sorted({_words(a) for a in n["aliases"]}),
            }
        )
    for kind, rows in by_kind.items():
        for b in _batches(rows):  # kind comes from the ENTITY_KINDS allowlist (checked above)
            _run(
                driver, database, f"UNWIND $rows AS r MERGE (e:Entity {{id: r.id}}) SET e:`{kind}`, e += r", {"rows": b}
            )

    for b in _batches(dataset["sources"]):
        _run(
            driver,
            database,
            "UNWIND $rows AS r MERGE (s:Source {id: r.id}) SET s.title = r.title, s.uri = r.uri, s.license = r.license",
            {"rows": b},
        )
    for b in _batches(dataset["chunks"]):
        _run(
            driver,
            database,
            """
            UNWIND $rows AS r
            MERGE (c:Chunk {id: r.id}) SET c.text = r.text, c.source_id = r.source_id
            WITH c, r MATCH (s:Source {id: r.source_id}) MERGE (c)-[:FROM_SOURCE]->(s)
            WITH c, r UNWIND r.mentions AS mid MATCH (e:Entity {id: mid}) MERGE (c)-[:MENTIONS]->(e)
        """,
            {"rows": b},
        )

    by_type: dict[str, list[dict[str, Any]]] = {}
    for e in dataset["edges"]:
        if e["type"] not in RELATIONSHIP_TYPES:
            raise SeedError(f"unknown relationship type {e['type']!r}")
        by_type.setdefault(e["type"], []).append(e)
    for rtype, rows in by_type.items():
        for b in _batches(rows):  # rtype comes from the RELATIONSHIP_TYPES allowlist (checked above)
            _run(
                driver,
                database,
                f"""
                UNWIND $rows AS r
                MATCH (a:Entity {{id: r.source}}), (b:Entity {{id: r.target}})
                MERGE (a)-[x:`{rtype}` {{id: r.id}}]->(b)
                SET x.statement = r.statement, x.chunk_id = r.chunk_id, x.source_id = r.source_id,
                    x.confidence = r.confidence
            """,
                {"rows": b},
            )

    if prune:
        _run(
            driver,
            database,
            "MATCH (e:Entity) WHERE NOT e.id IN $ids DETACH DELETE e",
            {"ids": [n["id"] for n in dataset["nodes"]]},
        )
        _run(
            driver,
            database,
            "MATCH (c:Chunk) WHERE NOT c.id IN $ids DETACH DELETE c",
            {"ids": [c["id"] for c in dataset["chunks"]]},
        )
        _run(
            driver,
            database,
            "MATCH (s:Source) WHERE NOT s.id IN $ids DETACH DELETE s",
            {"ids": [s["id"] for s in dataset["sources"]]},
        )
        _run(
            driver,
            database,
            "MATCH ()-[r]->() WHERE r.id IS NOT NULL AND NOT r.id IN $ids DELETE r",
            {"ids": [e["id"] for e in dataset["edges"]]},
        )

    m = dataset["manifest"]
    _run(
        driver,
        database,
        """
        MERGE (g:GraphMeta {id: 'current'})
        SET g.revision = $rev, g.dataset = $ds, g.seeded_checksum = $sha
    """,
        {"rev": m["graph_revision"], "ds": m["dataset"], "sha": json.dumps(m["sha256"], sort_keys=True)},
    )
    return validate(driver, database, dataset)


def validate(driver: neo4j.Driver, database: str, dataset: dict[str, Any]) -> dict[str, Any]:
    c = _run(
        driver,
        database,
        """
        CALL () { MATCH (e:Entity) RETURN count(e) AS nodes }
        CALL () { MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS edges }
        CALL () { MATCH (s:Source) RETURN count(s) AS sources }
        CALL () { MATCH (c:Chunk) RETURN count(c) AS chunks }
        CALL () { MATCH (:Entity)-[r]->(:Entity) WHERE r.chunk_id IS NULL OR NOT EXISTS { MATCH (:Chunk {id: r.chunk_id}) } RETURN count(r) AS unsourced }
        RETURN nodes, edges, sources, chunks, unsourced
    """,
    )[0]
    exp = dataset["manifest"]["counts"]
    problems = [f"{k}: {c[k]} != {exp[k]}" for k in ("nodes", "edges", "sources", "chunks") if c[k] != exp[k]]
    if c["unsourced"]:
        problems.append(f"{c['unsourced']} relationships lack a valid evidence chunk")
    if problems:
        raise SeedError("post-seed validation failed: " + "; ".join(problems))
    return {**c, "graph_revision": dataset["manifest"]["graph_revision"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="data/demo")
    ap.add_argument("--create-database", action="store_true")
    ap.add_argument("--provision-reader", action="store_true")
    ap.add_argument("--reader-user", default="graphrag_reader")
    ap.add_argument("--prune", action="store_true", help="delete graph items absent from the dataset")
    ap.add_argument("--reset", action="store_true", help="wipe the database first (local disposable only)")
    ap.add_argument("--allow-non-demo-database", action="store_true")
    ap.add_argument("--edition", choices=["enterprise", "community"], default="enterprise")
    args = ap.parse_args(argv)

    uri = os.environ.get("GRAPHRAG_SEED_URI", "bolt://127.0.0.1:27687")
    user = os.environ.get("GRAPHRAG_SEED_USER", "neo4j")
    password = os.environ.get("GRAPHRAG_SEED_PASSWORD")
    database = os.environ.get("GRAPHRAG_SEED_DATABASE", "demo")
    if not password:
        print("GRAPHRAG_SEED_PASSWORD is required", file=sys.stderr)
        return 2
    try:
        guard_database(
            uri, database, allow_non_demo=args.allow_non_demo_database, community=args.edition == "community"
        )
        if args.reset:
            guard_reset(uri)
        dataset = load_dataset(Path(args.dataset))
        with neo4j.GraphDatabase.driver(uri, auth=(user, password)) as driver:
            if args.create_database and args.edition == "enterprise":
                create_database(driver, database)
            if args.reset:
                _run(driver, database, "MATCH (n) DETACH DELETE n")
            report = seed(driver, database, dataset, prune=args.prune)
            if args.provision_reader and args.edition == "community":
                rpw = os.environ.get("GRAPHRAG_READER_PASSWORD")
                if not rpw:
                    print("GRAPHRAG_READER_PASSWORD is required with --provision-reader", file=sys.stderr)
                    return 2
                provision_community_user(driver, args.reader_user, rpw)
                report["reader_user"] = args.reader_user
            elif args.provision_reader:
                rpw = os.environ.get("GRAPHRAG_READER_PASSWORD")
                if not rpw:
                    print("GRAPHRAG_READER_PASSWORD is required with --provision-reader", file=sys.stderr)
                    return 2
                report["reader_role"] = provision_reader(driver, database, args.reader_user, rpw)
    except SeedError as e:
        print(f"seed refused: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
