# Architecture

```
MCP client(s) ── HTTP(S) ──► (optional host/proxy; local stack binds 127.0.0.1)
                              │  Authorization: Bearer <JWT>
                      ┌───────▼─────────────────────────────────────────────┐
                      │ ASGI app (uvicorn)                                  │
                      │  HostAllowlist ► JWT auth (401) ► RequireScope(403) │
                      │  FastMCP 4.2.5  /mcp (Streamable HTTP, stateless)   │
                      │  /health  /ready  /status(admin)                    │
                      │  tools.py  ── typed errors (INVALID_ARGUMENT …)     │
                      │  GraphToolService ── validate, rate/concurrency,    │
                      │      deadline, size bound, structured logs          │
                      │  Neo4jRepository ── fixed read-only Cypher          │
                      └───────┬─────────────────────────────────────────────┘
                              │ bolt(+s), READ routing, tx timeout, request_id metadata
                      Neo4j  database `demo`  (role: MATCH only, ACCESS only to that DB)
Offline: tools/seed_demo.py (admin creds, not in serving image) ► same database
```

## Components
| Module | Responsibility |
|---|---|
| `main.py` | wiring, lifespan (driver opened at start-up, closed on SIGTERM), middleware order |
| `auth.py` | `PrincipalJWTVerifier` (iss/aud/exp/sig via FastMCP `JWTVerifier`, + required `exp` and `sub`, scope derivation), 403 scope middleware, Host allowlist |
| `tools.py` | four tools, typed validation-error middleware, `ToolFailure` → MCP error JSON |
| `service.py` | `GraphToolService`: input hygiene, per-principal rate limit, concurrency semaphore, request deadline, response byte bound, log record |
| `repository.py` | the only Cypher in the server; relationship-type union built from the allowlist; hop bound from `{1,2,3,4}` |
| `schemas.py` | Pydantic contracts, error taxonomy, allowlists (kinds, relationship types, scopes) |
| `config.py` | typed settings; refuses to start without issuer/audience/key; non-local requires TLS Neo4j URI, non-admin DB user, SSRF-safe JWKS |
| `observability.py` | JSON logs (Cloud Logging severity/timestamp), request-id context, DB-time accumulator |
| `health.py` | liveness (no deps), readiness (bounded `RETURN 1`, no detail), admin status |

## Decisions (ADR-style)
1. **Atomic tools, no agent.** Clients compose; the server stays deterministic and testable. (SDD §4)
2. **No Cypher generation / `run_cypher` in v1.** `EXPLAIN` checks syntax, not authorisation (observed in the
   source project, see PROVENANCE). Safety comes from fixed templates *and* a read-only DB role, tested separately.
3. **Stateless HTTP (`stateless_http=True`).** Every request is self-contained, so any host (scale-to-zero or multi-instance)
   can route any request to any instance. Verified with three client stacks.
4. **Auth in the app even behind a host's own access check.** A host gate may guard the ingress; the app still validates issuer, audience,
   expiry, signature and scopes. There is no switch to disable auth.
5. **Real relationship types, not a single `REL` type.** Keeps the graph idiomatic; dynamic parts come only from
   allowlists, and a unit test pins the union to the allowlist.
6. **Community Neo4j by default (free, one command); Enterprise optional** for real RBAC (`GRANT MATCH` on one database, `DENY ACCESS` to the default DB).
   Managed-service equivalence is an open deployment item (COST_AND_LIMITS).
7. **Python 3.13 in the image** (3.11+ supported by `pyproject`); dependencies locked in `uv.lock`; image built
   from the lock with `--frozen --no-dev`.

## Graph model
`(:Entity {id,name,name_norm,name_words,name_tokens,aliases,alias_norms,alias_words,description}) + kind label`,
`(:Source {id,title,uri,license})`, `(:Chunk {id,text,source_id})`, `(:Chunk)-[:MENTIONS]->(:Entity)`,
`(:Chunk)-[:FROM_SOURCE]->(:Source)`, `(:GraphMeta {id:'current', revision})`, and typed relationships
`OWNS, PART_OF, IMPLEMENTED_BY, DEPENDS_ON, USES, PUBLISHES_TO, CONSUMES_FROM, INTEGRATES_WITH` each carrying
`id, statement, chunk_id, source_id, confidence`. Unique constraints on ids; range+text index on `name_norm`.
Dataset: 146 entities, 470 relationships, 25 sources, 95 chunks (generated, seed 20261123).
