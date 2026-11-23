"""Out-of-band HTTP endpoints: liveness, readiness and an admin-only status page."""

from __future__ import annotations

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import PrincipalJWTVerifier
from .config import Settings
from .repository import GraphRepository
from .schemas import CONTRACT_VERSION, SCOPE_ADMIN


def register_health(mcp: FastMCP, repo: GraphRepository, settings: Settings, verifier: PrincipalJWTVerifier) -> None:
    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:  # liveness: process is up; no dependencies touched
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/ready", methods=["GET"])
    async def ready(_: Request) -> JSONResponse:  # bounded backend check; reveals no detail
        ok = await repo.ping()
        return JSONResponse({"status": "ready" if ok else "unavailable"}, status_code=200 if ok else 503)

    @mcp.custom_route("/status", methods=["GET"])
    async def status(request: Request) -> JSONResponse:  # operator view; requires graph:admin
        header = request.headers.get("authorization", "")
        token = await verifier.verify_token(header[7:]) if header.lower().startswith("bearer ") else None
        if token is None:
            return JSONResponse({"error": "unauthenticated"}, status_code=401, headers={"www-authenticate": "Bearer"})
        if SCOPE_ADMIN not in set(token.scopes):
            return JSONResponse({"error": "insufficient_scope"}, status_code=403)
        return JSONResponse(
            {
                "service_version": settings.service_version,
                "git_sha": settings.git_sha,
                "cloud_revision": settings.cloud_revision or None,
                "environment": settings.environment,
                "contract_version": CONTRACT_VERSION,
                "graph_revision": await repo.graph_revision(),
                "limits": {
                    "query_timeout_s": settings.query_timeout_s,
                    "request_deadline_s": settings.request_deadline_s,
                    "max_response_bytes": settings.max_response_bytes,
                    "max_concurrency": settings.max_concurrency,
                    "rate_limit_per_minute": settings.rate_limit_per_minute,
                },
            }
        )
