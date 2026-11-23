"""Domain exceptions. Raised by service/repository, mapped to MCP errors in tools.py."""

from __future__ import annotations

from .schemas import ErrorCode


class ToolFailure(Exception):  # noqa: N818 - domain name
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = "-"


def invalid(message: str) -> ToolFailure:
    return ToolFailure(ErrorCode.INVALID_ARGUMENT, message)
