"""Structured API errors shared by every router."""
from __future__ import annotations

from typing import Any, Optional


class APIError(Exception):
    """An error with a stable machine-readable code and HTTP status."""

    def __init__(self, status_code: int, code: str, message: str,
                 detail: Optional[Any] = None):
        self.status_code = int(status_code)
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(message)


def error_body(code: str, message: str, detail: Optional[Any] = None) -> dict:
    body = {"error": {"code": code, "message": message}}
    if detail is not None:
        body["error"]["detail"] = detail
    return body
