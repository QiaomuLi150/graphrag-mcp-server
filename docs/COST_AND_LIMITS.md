# Cost and limits

**Total spend: $0.** Nothing paid is provisioned or required. The default stack uses only free, locally run software:
Docker, Neo4j **Community** 5.23 (GPLv3, free), Python/uv. No cloud billing, no paid API, no LLM calls.
Optional free-tier hosting is evaluated in [FREE_HOSTING.md](FREE_HOSTING.md) and was not used.

## Enforced limits (values in `config.py`, tested)
| Limit | Default | Where |
|---|---|---|
| Entities per search / neighbours / paths / hops | ≤10 / ≤20 / ≤10 / ≤4 | schema + service |
| Context characters | 500–12 000 | schema + service |
| Query / context text | 200 / 500 chars | schema + service |
| Response size | 64 000 bytes (truncates, sets `truncated`) | service |
| Neo4j transaction timeout / request deadline | 3 s / 8 s | repository, service |
| Rate limit | 120 requests/min per principal, **per instance, in memory** | service |
| In-flight graph calls | 16 per instance (fail-fast `RATE_LIMITED`) | service |

## Neo4j editions (honest trade-off)
| | Community (default, free) | Enterprise (`docker-compose.enterprise.yml`, evaluation licence, local only) |
|---|---|---|
| Read-only enforcement | READ access-mode transactions (server rejects writes with `AccessMode` error) + fixed parameterised templates; separate login from admin | **plus** a database-scoped role with only `MATCH`, `DENY ACCESS` to the default DB |
| Limitation | Community users are not privilege-restricted: a stolen reader password could write. Local-only, bound to 127.0.0.1 | Not redistributable; needs licence acceptance |
Do not run the Community configuration on an untrusted network.
