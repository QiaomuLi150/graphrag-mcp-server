# Runbook

## Local setup / teardown
See README quick start (`docker compose up -d --build --wait`). `docker compose down -v` discards **all** local data (the Neo4j image uses an anonymous volume
that otherwise survives container re-creation, including the first admin password).
Ports (127.0.0.1 only): Neo4j bolt 27687, Neo4j http 27474, server 28080 (all overridable via env).

## Secrets
* Local: generated per developer, kept in `.local/` (git-ignored) or exported env vars. Never commit, never paste in chat.
* Any hosted use: the host's secret store only, read-only credentials only.
* **Rotate the Neo4j reader password:** `ALTER USER graphrag_reader SET PASSWORD $new CHANGE NOT REQUIRED` (as admin) → add a new
  Secret Manager version → redeploy/restart the revision → disable the old secret version.
* **Rotate signing keys (local dev issuer):** `rm .local/dev_jwt_*.pem; python -m tools.dev_token init`; restart the server with the new public key.
  Google/OIDC keys rotate at the issuer; the server picks up new keys after the JWKS cache refresh (~30 s).

## Seeding / reseeding
`python -m tools.generate_demo` (deterministic) → `python -m tools.seed_demo [--prune]`. Idempotent; refuses non-`demo*`
databases unless `--allow-non-demo-database`; `--reset` only against a local URI. After seeding, `graph_revision`
changes and clients see it in `meta.graph_revision`.

## Health and triage
| Symptom | Check | Likely cause / action |
|---|---|---|
| 401 on everything | `WWW-Authenticate` header, token `aud/iss/exp` | wrong audience (must equal `GRAPHRAG_AUTH_AUDIENCE`), expired, wrong issuer/key |
| 403 `insufficient_scope` | token claims | principal not in `GRAPHRAG_AUTH_ALLOWED_PRINCIPALS` (Google tokens) or no `graph:read` scope |
| 421 | Host header | `GRAPHRAG_ALLOWED_HOSTS` does not list the service host |
| `DB_UNAVAILABLE` / `/ready` 503 | `/health` still 200 = process fine | Neo4j down/unreachable/credentials changed; look for `neo4j_unavailable` log events (error type only). Recovery is automatic once the DB returns (drill: ~17 s incl. DB boot) |
| `TIMEOUT` | log `db_latency_ms`, `tool_name` | slow traversal; reduce `max_hops`/check indexes; limits `GRAPHRAG_QUERY_TIMEOUT_S`, `GRAPHRAG_REQUEST_DEADLINE_S` |
| `RATE_LIMITED` | `client_id_hash` in logs | per-principal limit (default 120/min/instance) or concurrency cap |
| Request correlation | give the user's `request_id` | grep Cloud Logging for `request_id`; the same id is attached as Neo4j transaction metadata (`SHOW TRANSACTIONS`) |

Log fields per tool call: `timestamp, severity, service_version, git_sha, cloud_revision, environment, request_id, client_id_hash,
tool_name, success, error_code, total_latency_ms, db_latency_ms, result_count, truncated, graph_revision`. No raw queries, tokens or graph text.

## Recovery drill
Local DB outage + automatic recovery is recorded in `eval/results/outage_drill_local.json` (no restart of the server needed).

## Incident response (minimal)
Contain: stop the service/stack → investigate with `request_id`/`client_id_hash` →
rotate the reader password and affected tokens' issuer keys → redeploy → record in this file.

## Teardown
`docker compose down -v` (removes Neo4j data and generated dev keys).
