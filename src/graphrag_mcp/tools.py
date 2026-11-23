"""MCP tool registration. Thin: parse/authorise, delegate to GraphToolService, map errors."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError as McpToolError
from fastmcp.exceptions import ValidationError as McpValidationError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from .errors import ToolFailure
from .observability import get_logger, new_request_id
from .schemas import (
    MAX_CONTEXT_QUERY_CHARS,
    MAX_ID_CHARS,
    MAX_QUERY_CHARS,
    SCOPE_READ,
    ErrorCode,
    FindPathsResult,
    GetEntityResult,
    RetrieveContextResult,
    SearchEntitiesResult,
    ToolError,
)
from .service import GraphToolService

T = TypeVar("T", bound=BaseModel)

INSTRUCTIONS = (
    "Read-only knowledge graph tools over a synthetic enterprise demo corpus (teams, services, "
    "datastores, libraries, queues, products, features). Tools return bounded, source-attributed "
    "evidence; they never write. Use search_entities to find stable entity ids, then get_entity or "
    "find_relationship_paths. retrieve_graph_context links names in a free-text question to "
    "entities and returns supporting facts; if sufficient_evidence is false, no answer is supported."
)
_RO = {"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False, "openWorldHint": False}


log = get_logger("tools")


def _summarise(err: PydanticValidationError) -> str:
    """Compact, input-free description: 'field: reason; ...' (never echoes caller values)."""
    parts = []
    for d in err.errors(include_url=False, include_context=False, include_input=False)[:5]:
        loc = ".".join(str(x) for x in d["loc"] if x != "call[...]") or "arguments"
        parts.append(f"{loc}: {d['msg']}")
    return "; ".join(parts)


class TypedValidationErrors(Middleware):
    """Turn framework argument-validation failures into typed INVALID_ARGUMENT errors."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):  # type: ignore[no-untyped-def]
        try:
            return await call_next(context)
        except (McpValidationError, PydanticValidationError) as e:
            cause = e if isinstance(e, PydanticValidationError) else e.__cause__
            msg = _summarise(cause) if isinstance(cause, PydanticValidationError) else "invalid arguments"
            rid = new_request_id()
            log.info(
                "tool_call",
                tool_name=getattr(context.message, "name", "?"),
                success=False,
                error_code="INVALID_ARGUMENT",
            )
            raise _wire(ToolFailure(ErrorCode.INVALID_ARGUMENT, msg), rid) from None


def _principal() -> str:
    token = get_access_token()
    if token is None:
        raise _wire(ToolFailure(ErrorCode.UNAUTHENTICATED, "authentication required"))
    if SCOPE_READ not in set(token.scopes):
        raise _wire(ToolFailure(ErrorCode.PERMISSION_DENIED, f"scope {SCOPE_READ} required"))
    return token.client_id or "unknown"


def _wire(f: ToolFailure, request_id: str | None = None) -> McpToolError:
    body = ToolError(code=f.code, message=f.message, retryable=f.retryable, request_id=request_id or f.request_id)
    return McpToolError(body.model_dump_json())


async def _guard(call: Callable[[str], Awaitable[T]]) -> T:
    principal = _principal()
    try:
        return await call(principal)
    except ToolFailure as f:
        raise _wire(f) from None


def build_server(service: GraphToolService, auth: AuthProvider, **kwargs: object) -> FastMCP:
    mcp = FastMCP("graphrag-mcp", instructions=INSTRUCTIONS, auth=auth, mask_error_details=True, **kwargs)  # type: ignore[arg-type]

    @mcp.tool(annotations=_RO)
    async def search_entities(
        query: Annotated[
            str, Field(min_length=1, max_length=MAX_QUERY_CHARS, description="Entity name, alias or fragment.")
        ],
        labels: Annotated[
            list[str] | None, Field(description="Optional entity kinds, e.g. Service, Team.", max_length=7)
        ] = None,
        limit: Annotated[int, Field(ge=1, le=10, description="Maximum entities to return.")] = 5,
    ) -> SearchEntitiesResult:
        """Find entities by normalised name or alias (exact, then prefix, then substring)."""
        return await _guard(lambda p: service.search_entities(p, query, labels, limit))

    @mcp.tool(annotations=_RO)
    async def get_entity(
        entity_id: Annotated[str, Field(min_length=1, max_length=MAX_ID_CHARS, description="Stable entity id.")],
        neighbor_limit: Annotated[int, Field(ge=0, le=20, description="Maximum neighbours to include.")] = 10,
    ) -> GetEntityResult:
        """Return one entity by stable id with attributed, bounded neighbour summaries."""
        return await _guard(lambda p: service.get_entity(p, entity_id, neighbor_limit))

    @mcp.tool(annotations=_RO)
    async def find_relationship_paths(
        source_entity_id: Annotated[str, Field(min_length=1, max_length=MAX_ID_CHARS)],
        target_entity_id: Annotated[str, Field(min_length=1, max_length=MAX_ID_CHARS)],
        max_hops: Annotated[int, Field(ge=1, le=4, description="Maximum relationship hops.")] = 3,
        max_paths: Annotated[int, Field(ge=1, le=10, description="Maximum paths to return.")] = 5,
    ) -> FindPathsResult:
        """Find simple (node-distinct) relationship paths between two entities, shortest first."""
        return await _guard(
            lambda p: service.find_relationship_paths(p, source_entity_id, target_entity_id, max_hops, max_paths)
        )

    @mcp.tool(annotations=_RO)
    async def retrieve_graph_context(
        query: Annotated[
            str, Field(min_length=1, max_length=MAX_CONTEXT_QUERY_CHARS, description="Free-text question or topic.")
        ],
        top_k: Annotated[int, Field(ge=1, le=10, description="Maximum linked entities.")] = 5,
        max_chars: Annotated[int, Field(ge=500, le=12000, description="Character budget for snippets.")] = 6000,
    ) -> RetrieveContextResult:
        """Link entities named in a question and return supporting graph facts with provenance."""
        return await _guard(lambda p: service.retrieve_graph_context(p, query, top_k, max_chars))

    mcp.add_middleware(TypedValidationErrors())
    _ = (search_entities, get_entity, find_relationship_paths, retrieve_graph_context)
    return mcp


def parse_tool_error(text: str) -> ToolError:
    """Client helper: decode the JSON error body carried in an MCP tool error."""
    return ToolError.model_validate(json.loads(text))
