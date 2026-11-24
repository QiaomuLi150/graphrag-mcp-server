# Release evidence ledger

**Verdict: COMPLETE as a local, open-source release.** Budget $0; nothing paid or cloud-hosted. No remote deployment is claimed.
The exact commit of a release is recorded in the GitHub release/tag notes and `git log` (a file inside a commit cannot contain its own hash).

## Protocol
**MCP `2026-07-28`** — `server/discover`, required per-request `_meta`, no `initialize`, no session id; legacy handshake clients accepted as compatibility.
Verified on the raw wire and with a real client; two-process/no-sticky-session run: 0 errors, both processes served traffic. See [PROTOCOL.md](PROTOCOL.md).

## Checks (local, all re-runnable)
| Check | Result |
|---|---|
| `pytest` on Neo4j Community (default stack) | **117 passed, 8 skipped** (7 need Enterprise RBAC, 1 deployment-only; reasons printed) |
| `pytest` on Neo4j Enterprise (optional compose) | 115 passed / 1 skipped before the protocol tests were added; protocol tests are edition-independent |
| `ruff check`, `ruff format --check`, `mypy src tools` | clean |
| `bandit -r src -ll` | 0 findings (tools/eval scripts: only subprocess/`assert`/seeded-PRNG notes, not shipped in the runtime image) |
| `pip-audit` on locked runtime dependencies | no known vulnerabilities (2026-11-23, PyPI/OSV data) |
| Dependency licences (67 runtime packages) | no GPL/AGPL/LGPL/SSPL; MIT/BSD/Apache/MPL/Unlicense; 2 platform-only packages (pywin32, httpx2-jsfetch) not installed on Linux |
| History audit (`python -m tools.audit_history`, every commit and blob) | see [audit/HISTORY_AUDIT.md](audit/HISTORY_AUDIT.md) |
| Docker Compose one-command start from a fresh clone + demo | PASS |
| Gold evaluation (113 synthetic cases) via MCP | 113/113 — synthetic, templated; not evidence of real-world retrieval quality |
| GitHub Actions (free `ubuntu-24.04` runners, private repo) | **All 5 jobs green**: static-and-unit (ruff, format, mypy, bandit, history audit, unit), integration (Neo4j Community service), compose-e2e (one-command stack + demo), container (non-root/no-seeder checks + Trivy HIGH/CRITICAL), supply-chain (gitleaks full history, pip-audit). Run ids/URLs: repository Actions tab |
| Trivy / gitleaks | CI only. First Trivy run found 5 HIGH items in the image (OS zlib + pip/setuptools-vendored libs); fixed by patching OS packages and removing pip/setuptools from the runtime image; rescan clean |

## Tested capabilities vs. unsupported claims
Tested: 4 typed bounded read-only tools; JWT auth matrix; READ-mode write protection (all editions) and role-based protection (Enterprise); typed errors;
abstention; stateless multi-process operation; one-command local stack; three client stacks produce identical results.
Not claimed: cloud/remote deployment, production use, high availability, scale, enterprise adoption, accuracy/latency uplift vs. alternatives, MCP Inspector interop.

## Defects found and fixed during validation
(1) missing `sub` accepted; (2) direct fact dropped by per-entity cap; (3) Cypher ordering syntax; (4) Neo4j anonymous-volume password persistence;
(5) strict Host check would break probes; (6) benchmark invalidated by default rate limit; (7) SHA-1 id hashing flagged by bandit (now `usedforsecurity=False`, output unchanged).
