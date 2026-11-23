# Pre-publication audit

Tool: `python -m tools.audit_history` (every commit and every distinct blob; patterns for private keys, cloud/API tokens, JWTs, credentialed URLs,
Aura hostnames, local paths, e-mail addresses, sensitive words, high-entropy strings; line-level overlap against the third-party tutorial checkout).
Run 2026-11-23. Dependency licences: [dependency_licenses.json](dependency_licenses.json).

## 1. Original local history (9 commits before publication)
| Area | Result |
|---|---|
| Secrets (keys, tokens, JWTs, credentialed URIs, Aura hosts) | **None found in any commit.** Local dev keys/passwords lived only in git-ignored `.local/`; compose defaults are documented local-only values |
| Private/internal data | None. Dataset is generated (`tools/generate_demo.py`); no customer, employer or proprietary material |
| Personal identifiers | **Finding (MEDIUM):** all original commits carried a personal e-mail address in author/committer metadata. **Resolved by publishing a fresh history** under the GitHub no-reply identity |
| Employer name in disclaimer text | **Finding (LOW):** early docs named an employer only to disclaim any relationship. Removed; publishing a fresh history also drops the old text |
| Third-party source copied | **None.** Line-overlap vs. the tutorial repo (no licence file) found 1 generic Pydantic idiom line (`aliases: list[str] = Field(default_factory=list)`), judged non-copying; no file overlaps ≥5 lines |
| Licence conflicts | Project is MIT. 67 runtime dependencies: no GPL/AGPL/LGPL/SSPL; Neo4j Community (GPLv3) and Enterprise (evaluation licence) are only *pulled by the user* via compose, never redistributed here |

## 2. Published history
The public repository starts from one squashed commit authored as the GitHub no-reply address; re-running the audit on it reports **0 secret, 0 personal-identifier and 0 sensitive-word findings** and the single LOW overlap above.
The original granular history stays in the author's local repository.
