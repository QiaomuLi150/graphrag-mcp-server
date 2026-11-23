# Contributing

```bash
uv sync --frozen
docker compose up -d neo4j                      # Community edition; integration tests need a live Neo4j
export GRAPHRAG_TEST_NEO4J_ADMIN_PASSWORD=local-dev-admin-pw GRAPHRAG_REQUIRE_NEO4J=1
uv run ruff check . && uv run ruff format --check . && uv run mypy src tools && uv run pytest
```
Integration tests seed the single Community database and refuse to run if it holds non-demo data. With Neo4j Enterprise
(`docker-compose.enterprise.yml`) the tests use a disposable `gr-test-*` database and also exercise the role-based read-only checks.
Do not add tools that execute caller-supplied Cypher. Keep new Cypher parameterised and covered by a hostile-input test.
