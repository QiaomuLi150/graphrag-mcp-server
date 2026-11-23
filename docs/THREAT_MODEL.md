# Threat model

**Assets:** graph contents (synthetic here, but treated as if sensitive), Neo4j credentials, bearer tokens,
cloud billing budget, service availability. **Adversaries:** unauthenticated internet users; authenticated
callers exceeding privileges; malicious tool arguments; prompt-injected source text; compromised credentials.
Every control below names the automated test that exercises it (`tests/...`).

| # | Threat | Control | Evidence |
|---|---|---|---|
| T1 | Unauthenticated access | App-level JWT verification (401) | `security/test_missing_token_is_401`, `non_bearer_scheme`, `garbage_tokens`, `token_in_query_string_is_ignored` |
| T2 | Token forgery / replay | Issuer, audience, signature, `exp` (required), `nbf`, `sub` (required); fixed algorithm RS256 | `invalid_claims_are_401[expired, wrong_audience, wrong_issuer, missing_exp, missing_sub, not_yet_valid]`, `forged_signature`, `alg_none`, `hs256_with_public_key_as_secret` |
| T3 | Privilege escalation | Scope `graph:read` required (HTTP 403 + tool-level check); admin endpoint needs a configured admin principal, not a self-asserted scope | `valid_token_without_graph_read_scope_is_403`, `status_requires_admin_scope`, `principal_allowlist_grants_scope…` (unverified email gets nothing) |
| T4 | Cypher injection | No user text in Cypher strings; values are parameters; relationship types/hops come from code allowlists; no `run_cypher` | `integration/test_hostile_text_is_treated_as_data` (7 payloads), `unit/test_cypher_relationship_union…`, `protocol/test_no_unrestricted_cypher_tool_is_exposed` |
| T5 | Writes to the graph | Read routing + database role with only `ACCESS` + `MATCH` on one database; server holds no admin credentials; seeder not in image | `integration/test_runtime_principal_cannot_write[6 statements]`, `…cannot_read_other_databases`, `unit/test_serving_image_does_not_ship_seeder` |
| T6 | Resource exhaustion (query) | Hop ≤ 4, paths ≤ 10, list caps, char budgets, server-side tx timeout, request deadline, response-byte cap | `unit/test_invalid_arguments_never_reach_the_database`, `protocol/test_limits_rejected_over_wire…`, `integration/test_server_side_deadline_maps_to_timeout`, `unit/test_response_size_bound…` |
| T7 | Resource exhaustion (traffic) | Per-principal sliding-window rate limit, concurrency cap with fail-fast `RATE_LIMITED`, host instance limits | `unit/test_rate_limit_per_principal`, `…concurrency_cap…` (limits are per instance: see limitations) |
| T8 | Information leakage in errors/logs | Sanitised typed errors (no stack traces, URIs, driver messages); logs never contain tokens, query text or passwords | `unit/…unexpected_exception_becomes_internal_error…`, `integration/test_unreachable_database_is_sanitised`, `security/test_logs_correlate…never_contain_tokens_or_query_text`, `security/test_status_for_listed_admin_has_no_secrets` |
| T9 | Host-header / DNS-rebinding | `GRAPHRAG_ALLOWED_HOSTS` allowlist on the MCP path (421) | `security/test_host_header_allowlist…` |
| T10 | SSRF via provenance/URIs or JWKS | Only `demo://` source locators are ever returned; no fetch tools; JWKS fetch is HTTPS/public-address-only outside `local` | `integration/test_search_limit…` (uri check), `security/test_ssrf_guard_cannot_be_disabled_outside_local` |
| T11 | Prompt injection through graph text | No LLM in the request path; tool output is data. Clients must still treat graph text as untrusted (documented) | design; synthetic corpus |
| T12 | Credential leakage in repo/CI | Gitleaks in CI, pattern test over tracked files, `.env`/`.local`/`*.pem` ignored, deploy uses Workload Identity Federation (no keys) | `unit/test_repo_hygiene.py` |
| T13 | Supply chain | Locked deps (`uv.lock`), `pip-audit` + Trivy in CI, non-root image | `.github/workflows/ci.yml` (**not yet executed on GitHub**) |
| T14 | Dev auth left on in release | There is no disable switch; `Settings` refuses to start without issuer/audience/key; non-local requires TLS Neo4j URI, non-admin user, SSRF-safe JWKS | `unit/test_no_auth_disable_switch_exists`, `test_no_auth_bypass_in_source`, `test_non_local_requires_tls…` |

## Residual risks and limitations (not hidden)
* Rate limiting and concurrency caps are **per instance, in memory**. Under scale-out the effective limit is
  `limit × instances`; host instance limits bound this. A shared limiter is future work.
* JWKS key rotation: the verifier caches keys (≈30 s refresh); a new key is rejected until the cache refreshes.
* The `/ready` and `/health` endpoints are unauthenticated by design (probes); they reveal only `ok/ready/unavailable`.
* Tokens are bearer tokens: anyone holding a valid one can call the tools until it expires. Use short TTLs.
* Tokens carry no per-tool or per-tenant authorisation beyond `graph:read`; scope derivation can use a principal allowlist.
* Authentication against real third-party OIDC issuers is not exercised; the JWKS/`kid` path is tested with a local JWKS server only.
* With Neo4j **Community** (default local stack) the database offers no role boundary: write protection relies on READ access mode + fixed templates (see COST_AND_LIMITS). Enterprise adds a real read-only role.
* No penetration test, fuzzing campaign or load-based DoS test beyond the tests listed was performed.
