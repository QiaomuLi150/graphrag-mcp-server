# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.13-slim

# ---- build: resolve the locked dependency set into a self-contained virtualenv ----------------
FROM ${PYTHON_IMAGE} AS build
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.10.7
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && /app/.venv/bin/python -m compileall -q /app/.venv/lib

# ---- seeder: privileged, offline tooling. NEVER deployed to a host. -------------------------
FROM build AS seeder
COPY tools ./tools
COPY eval ./eval
COPY data ./data
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app
ENTRYPOINT ["python", "-m", "tools.seed_demo"]

# ---- runtime: the only image that serves traffic ----------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ARG GIT_SHA=unknown
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    GRAPHRAG_GIT_SHA=${GIT_SHA} GRAPHRAG_HOST=0.0.0.0 GRAPHRAG_PORT=8080
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
USER 10001
EXPOSE 8080
# No data/, tools/, tests/ or seeding code in this image: it cannot write to the graph by design.
CMD ["python", "-m", "graphrag_mcp.main"]
