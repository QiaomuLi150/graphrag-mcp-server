# Evaluation

Everything here was measured on **one local environment**: the server in a non-root, read-only Docker container, Neo4j 5.23
Enterprise in another container, clients on the same host (a shared 256-CPU machine with a load average of ~25 during
the runs). Numbers describe that setup only. **None come from a cloud deployment.**
Raw machine-readable reports with configuration, gold-set hash and host info: [`eval/results/`](../eval/results/).

## 1. Test suite (live Neo4j, real protocol; no mocks counted as integration)
| Suite | Tests | What it proves |
|---|---|---|
| unit | 28 | normalisation, determinism of dataset, allowlists, config fail-closed, service logic against an in-memory **fake** repository (mock, labelled), hygiene/secret scan |
| integration | 37 | real Neo4j reads vs independent BFS/DFS oracle on the source data, groundedness of snippets in stored chunk text, read-only role denies 6 write/DDL statements and other databases, 7 hostile-input payloads, DB outage/credential/timeout mapping, idempotent seeding, seeder guards |
| protocol | 20 | initialize/list/call over Streamable HTTP with **three client stacks** (FastMCP client, official `mcp` SDK, hand-written raw JSON-RPC) returning identical schemas/results; typed errors; limits rejected over the wire; concurrency |
| security | 26 | 401/403 matrix (expired, wrong aud/iss, forged key, `alg=none`, HS256 key-confusion, missing exp/sub, nbf, garbage, query-string token), admin endpoint, scope allow-list, log hygiene, Host allowlist, JWKS `kid` path |
| smoke | 1 | black-box deployment check; **skipped** unless `GRAPHRAG_SMOKE_URL/TOKEN` set (it is run separately via `eval.smoke`, see below) |

Final run: **111 passed, 1 skipped (the deployment smoke test, by design)**, in the working tree and again in a fresh `git clone`
against a freshly created Compose stack (`ruff` clean, `mypy` clean on `src` and `tools`). The skip is not counted as a pass.

## 2. Gold evaluation (113 synthetic, templated cases; oracles computed from `data/demo` in plain Python, independent of Neo4j/server code)
Split: 46 dev / 31 frozen / 36 unseen (deterministic SHA-256 assignment within each of 9 categories).
Final results through MCP (container) and through direct service calls were **identical: 113/113 correct**:

| Metric | Value |
|---|---|
| tool_success_rate (valid, authorised calls) | 1.000 |
| error-handling accuracy (12 malformed/hostile/not-found cases return the expected typed code) | 1.000 |
| entity precision@k / recall@k | 1.000 / 1.000 |
| path recall@k (exact top-k list incl. ordering vs exhaustive DFS oracle) / shortest path correct | 1.000 / 1.000 |
| no-path correct rejection (island + hop-limit cases) | 1.000 (9 cases) |
| evidence precision (snippet text literally inside cited chunk) / source attribution coverage | 1.000 / 1.000 |
| correct abstention on unanswerable questions | 1.000 (6 cases) |
| per-split accuracy dev / frozen / unseen | 1.000 / 1.000 / 1.000 |

**How to read this honestly**
* The corpus and the questions are synthetic and templated from the same facts; 100 % shows the implementation agrees with an independent
  oracle on this data. It is **not** evidence about messy real-world retrieval quality.
* History: the first all-split run (`run1_direct_all_splits_before_fix.json`) scored 112/113. The single failure (`context-0084`, a **dev** case)
  exposed a real defect: the per-entity fact cap could drop the *direct* fact between two linked entities when one had many neighbours.
  Fixed (direct facts are ordered first) with a regression test; rerun 113/113. Frozen/unseen cases were executed in that first run (aggregate
  only, no failures there) and were not used to design the fix, but they are therefore **not pristine held-out data**.
* Several gold categories are easy by construction (exact names). Ambiguity, aliases, hop limits and abstention are the more telling ones.

## 3. Baselines
* **Direct repository/service call vs MCP** (same logic): see §4 for protocol overhead. MCP did not change any result.
* **Lexical TF-IDF chunk retrieval vs graph context** (11 context cases, same 6 000-char budget): both retrieved the gold fact in 11/11. The graph tool
  returned 694 characters on average vs 5 844 for the lexical baseline (which fills the budget with partly irrelevant chunks), and additionally
  returns linked entity ids, paths and per-fact provenance. The questions favour both methods; this is a floor, not a quality claim. (`baseline_lexical_vs_graph.json`)
* The original repository's `entity_info_by_name`/Text-to-Cypher flow was **not** benchmarked: it targets a different schema and executes generated Cypher; no comparable disposable setup exists.

## 4. Latency and concurrency (warm; 400 requests per level, 101 valid gold calls cycled, 0 errors)
Client-observed end-to-end ms, p50 / p95 / p99. Load average ≈ 25 on the host during the run.

| Concurrency | Direct service p50/p95/p99 | Direct req/s | MCP (container) p50/p95/p99 | MCP req/s | MCP error rate |
|---|---|---|---|---|---|
| 1 | 2.71 / 9.14 / 20.6 | 261 | 7.08 / 16.2 / 21.67 | 118 | 0.0 |
| 4 | 4.76 / 16.63 / 26.9 | 585 | 16.0 / 39.97 / 160.12 | 189 | 0.0 |
| 8 | 9.89 / 36.54 / 98.47 | 527 | 26.95 / 85.53 / 212.98 | 214 | 0.0 |

Protocol/HTTP overhead at concurrency 1 ≈ +4.4 ms at p50. Single-container throughput at c=8 ≈ 214 req/s. The tail (p99) is dominated by noisy shared-host
scheduling and should be re-measured on the target platform before any budget is proposed (SDD: no latency guarantee before baselining).
An earlier benchmark attempt reported 61–100 % errors: that was the default per-principal rate limit (120/min) working as designed; the benchmark
container was re-created with the limit raised and rerun.

## 5. Cold start (local container, 5 runs; **not** a hosted platform)
Container start → `/ready` 200: 2.2–2.3 s. First client session + call: 307–482 ms; second: 28–37 ms (session setup and first Neo4j connection dominate).

## 6. Reliability drill (local)
Neo4j container stopped with the server running: tool call → `DB_UNAVAILABLE` (retryable, sanitised) after 7.3 s (3 s retry window + connect timeouts,
inside the 8 s deadline); `/health` 200, `/ready` 503. After restart the first successful call came 17.2 s later (includes Neo4j boot). No server restart needed.
Consider lowering `GRAPHRAG_NEO4J_MAX_RETRY_S` if a faster failure is preferred.

## 7. Acceptance thresholds (SDD §11.5)
| Threshold | Status |
|---|---|
| 100 % expected denials on security-negative tests | **PASS (local)**: every negative case in the security suite (missing, malformed, expired, wrong audience/issuer, forged key, `alg=none`, HS256 key-confusion, missing `exp`/`sub`, `nbf`, query-string token, insufficient scope, bad Host) returned 401/403/421; 26/26 tests pass |
| 100 % contract validity of registered examples/schemas | **PASS**: examples generated from the live server; 3 clients see identical schemas |
| ≥95 % deterministic correctness on frozen fixtures | **PASS (local, synthetic)**: 31/31 frozen, 36/36 unseen |
| All critical tests pass, zero unexplained skips | **PASS**: 111 pass; the one skip is the documented deployment-only test |
| P50/P95 over ≥100 warm requests with concurrency/cold start reported | **PASS (local only)**; not measured on any hosted platform |
| Two independent real clients against the **deployed** authenticated service | **NOT ATTEMPTED**: remote hosting is optional and unverified (FREE_HOSTING.md) |
| Rollback and restoration exercised in staging | **NOT ATTEMPTED** (cloud); local outage/recovery drill done |

## 8. Reproduce
```bash
python -m eval.build_gold                                  # regenerate (hash-checked) gold set
python -m eval.evaluate --mcp-url URL --token-env TOKEN --out eval/results/x.json
python -m eval.evaluate --direct --out eval/results/y.json  # needs GRAPHRAG_NEO4J_* read-only env
python -m eval.load_test --mcp-url URL --levels 1,4,8 --requests 400 --out eval/results/load.json
python -m eval.baseline_lexical --mcp-url URL --out eval/results/baseline.json
python -m eval.outage_drill   # local compose only
```
