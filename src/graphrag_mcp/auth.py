"""Bearer-token authentication and scope authorisation (fail closed).

Authentication is always performed in the application, even when a hosting platform also
guards the ingress: issuer, audience, expiry and signature are validated here.
There is intentionally NO switch that disables authentication.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings
from .observability import get_logger
from .schemas import SCOPE_ADMIN, SCOPE_READ

log = get_logger("auth")


class PrincipalJWTVerifier(JWTVerifier):
    """JWT verifier that also derives the effective scopes of a principal.

    * Tokens carrying a ``scope``/``scp`` claim keep exactly those scopes.
    * Tokens with no scope claim (e.g. Google-signed ID tokens) receive ``graph:read``
      only if their *verified* email or ``sub`` is on the configured allowlist.
    """

    def __init__(self, settings: Settings) -> None:
        kwargs: dict[str, Any] = {
            "issuer": settings.auth_issuer,
            "audience": settings.auth_audience,
            "algorithm": settings.auth_algorithm,
        }
        if settings.auth_jwks_uri:
            kwargs.update(jwks_uri=settings.auth_jwks_uri, ssrf_safe=settings.auth_jwks_ssrf_safe)
        else:
            kwargs["public_key"] = settings.auth_public_key
        super().__init__(**kwargs)
        self._allowed = settings.principals(settings.auth_allowed_principals)
        self._admins = settings.principals(settings.auth_admin_principals)

    async def verify_token(self, token: str) -> AccessToken | None:
        at = await super().verify_token(token)
        if at is None:
            return None
        if at.expires_at is None:  # tokens must expire
            log.warning("token_rejected", reason="missing_exp")
            return None
        claims = at.claims
        sub = str(claims.get("sub") or "")  # never fall back to a synthesised client id
        if not sub:
            log.warning("token_rejected", reason="missing_sub")
            return None
        identities = {sub.casefold()}
        if claims.get("email_verified") is True and claims.get("email"):
            identities.add(str(claims["email"]).casefold())
        scopes = set(at.scopes)
        if not scopes and identities & self._allowed:
            scopes.add(SCOPE_READ)
        if identities & self._admins:
            scopes.update({SCOPE_READ, SCOPE_ADMIN})
        return at.model_copy(update={"scopes": sorted(scopes), "client_id": sub})


class RequireScopeMiddleware:
    """Return HTTP 403 (insufficient_scope) for authenticated callers lacking ``scope``.

    Unauthenticated requests pass through untouched: the transport's auth layer answers
    them with 401. Runs after the authentication middleware.
    """

    def __init__(self, app: ASGIApp, *, path: str, scope: str = SCOPE_READ) -> None:
        self.app = app
        self.path = path
        self.required = scope

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == self.path.rstrip("/"):
            user = scope.get("user")
            token = getattr(user, "access_token", None)
            if token is not None and self.required not in set(token.scopes):
                body = json.dumps(
                    {"error": "insufficient_scope", "error_description": f"requires {self.required}"}
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (
                                b"www-authenticate",
                                f'Bearer error="insufficient_scope", scope="{self.required}"'.encode(),
                            ),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class HostAllowlistMiddleware:
    """Reject MCP requests whose Host header is not allow-listed (421). No-op when the list is empty.

    Applies only to the MCP path so a host's internal health/startup probes (which may use an
    internal Host value) are unaffected.
    """

    def __init__(self, app: ASGIApp, *, path: str, hosts: list[str]) -> None:
        self.app = app
        self.path = path.rstrip("/")
        self.hosts = {h.casefold() for h in hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.hosts and scope["type"] == "http" and scope["path"].rstrip("/") == self.path:
            host = dict(scope["headers"]).get(b"host", b"").decode("latin-1").casefold().rsplit(":", 1)[0]
            if host not in self.hosts:
                body = b'{"error":"misdirected_request"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 421,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
