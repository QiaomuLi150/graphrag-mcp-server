"""Application wiring and process entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware

from .auth import HostAllowlistMiddleware, PrincipalJWTVerifier, RequireScopeMiddleware
from .config import Settings, get_settings
from .health import register_health
from .observability import configure_logging, get_logger
from .repository import GraphRepository, Neo4jRepository
from .service import GraphToolService
from .tools import build_server


def create_app(settings: Settings | None = None, repository: GraphRepository | None = None) -> Starlette:
    settings = settings or get_settings()
    configure_logging(
        settings.log_level,
        {
            "service_version": settings.service_version,
            "git_sha": settings.git_sha,
            "cloud_revision": settings.cloud_revision or None,
            "environment": settings.environment,
        },
    )
    repo: GraphRepository = repository or Neo4jRepository(settings)
    service = GraphToolService(repo, settings)
    verifier = PrincipalJWTVerifier(settings)

    @asynccontextmanager
    async def lifespan(_: FastMCP[Any]) -> AsyncIterator[None]:
        await repo.connect()  # driver is created at start-up, never at import time
        get_logger("main").info("started", mcp_path=settings.mcp_path)
        try:
            yield
        finally:
            await repo.close()  # graceful shutdown: release pooled connections

    mcp = build_server(service, verifier, lifespan=lifespan)
    register_health(mcp, repo, settings, verifier)
    allowed_hosts = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]
    return mcp.http_app(
        path=settings.mcp_path,
        stateless_http=True,
        allowed_hosts=allowed_hosts or None,
        middleware=[
            Middleware(HostAllowlistMiddleware, path=settings.mcp_path, hosts=allowed_hosts),
            Middleware(RequireScopeMiddleware, path=settings.mcp_path),
        ],
    )


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,  # access logs would carry URLs; we log structured tool calls instead
        proxy_headers=True,
        timeout_graceful_shutdown=8,
    )


if __name__ == "__main__":
    main()
