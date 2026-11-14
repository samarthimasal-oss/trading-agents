"""Bearer-token auth and mutation guards for the local dashboard."""
from __future__ import annotations

import secrets
from typing import Callable

from fastapi import Request

from api.errors import APIError


def _supplied_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-dashboard-token", "").strip()


def token_matches(expected: str, supplied: str) -> bool:
    if not expected or not supplied:
        return False
    return secrets.compare_digest(str(expected), str(supplied))


def require_auth(request: Request) -> None:
    settings = request.app.state.services.settings
    if not token_matches(settings.dashboard_token, _supplied_token(request)):
        raise APIError(401, "unauthorized",
                       "a valid dashboard bearer token is required")


def require_mutation(request: Request) -> None:
    if request.headers.get("x-dashboard-request", "") != "1":
        raise APIError(400, "missing_request_header",
                       "mutating calls require X-Dashboard-Request: 1")


def idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "").strip()
    if not key:
        raise APIError(400, "missing_idempotency_key",
                       "mutating calls require an Idempotency-Key header")
    if len(key) > 200:
        raise APIError(400, "invalid_idempotency_key",
                       "Idempotency-Key must be at most 200 characters")
    return key


def run_idempotent(request: Request, action: str,
                   fn: Callable[[], dict]) -> dict:
    """Return a cached result for a repeated key, else execute once."""
    key = idempotency_key(request)
    store = request.app.state.services.store
    try:
        cached = store.get_idempotency(key, action)
    except ValueError as exc:
        raise APIError(409, "idempotency_conflict", str(exc)) from exc
    if cached is not None:
        return {**cached, "replayed": True}
    result = fn()
    store.save_idempotency(key, action, result)
    return result


__all__ = [
    "idempotency_key",
    "require_auth",
    "require_mutation",
    "run_idempotent",
    "token_matches",
]
