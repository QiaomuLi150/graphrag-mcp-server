"""Typed tool contracts (v1), error taxonomy, and code-defined allowlists.

Every value that is ever interpolated into a Cypher string (relationship types, hop
counts) comes from the constants in this module, never from caller input.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.0"

# --- allowlists -----------------------------------------------------------------------
ENTITY_KINDS: tuple[str, ...] = (
    "Team",
    "Product",
    "Service",
    "Datastore",
    "Library",
    "Queue",
    "Feature",
)
RELATIONSHIP_TYPES: tuple[str, ...] = (
    "OWNS",
    "PART_OF",
    "IMPLEMENTED_BY",
    "DEPENDS_ON",
    "USES",
    "PUBLISHES_TO",
    "CONSUMES_FROM",
    "INTEGRATES_WITH",
)
SCOPE_READ = "graph:read"
SCOPE_ADMIN = "graph:admin"

# --- bounds (single source of truth; also used in the JSON schema) ---------------------
MAX_QUERY_CHARS = 200
MAX_CONTEXT_QUERY_CHARS = 500
MAX_ID_CHARS = 128


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolError(_Model):
    """Wire shape of a failed tool call (JSON-encoded in the MCP error text)."""

    code: ErrorCode
    message: str
    retryable: bool = False
    request_id: str


class EvidenceRef(_Model):
    source_id: str
    chunk_id: str | None = None
    source_uri: str | None = None


class GraphEntity(_Model):
    id: str
    name: str
    labels: list[str]
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class NeighborSummary(_Model):
    relationship_id: str
    relationship_type: str
    direction: Literal["out", "in"]
    entity_id: str
    entity_name: str
    entity_labels: list[str]
    statement: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class GraphPath(_Model):
    node_ids: list[str]
    node_names: list[str]
    relationship_ids: list[str]
    relationship_types: list[str]
    directions: list[Literal["out", "in"]]
    evidence: list[EvidenceRef] = Field(default_factory=list)


class ContextSnippet(_Model):
    text: str
    entity_ids: list[str]
    relationship_id: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class ToolMeta(_Model):
    request_id: str
    graph_revision: str
    truncated: bool
    latency_ms: float | None = None
    contract_version: str = CONTRACT_VERSION


# --- tool inputs/outputs ---------------------------------------------------------------
class SearchEntitiesResult(_Model):
    entities: list[GraphEntity]
    meta: ToolMeta


class GetEntityResult(_Model):
    entity: GraphEntity
    neighbors: list[NeighborSummary]
    total_neighbors: int
    meta: ToolMeta


class FindPathsResult(_Model):
    paths: list[GraphPath]
    meta: ToolMeta


class RetrieveContextResult(_Model):
    snippets: list[ContextSnippet]
    linked_entities: list[GraphEntity]
    paths: list[GraphPath]
    sufficient_evidence: bool
    meta: ToolMeta


Limit10 = Annotated[int, Field(ge=1, le=10)]
