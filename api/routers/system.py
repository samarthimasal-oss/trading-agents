"""Public system routes plus authenticated overview and logs."""
from __future__ import annotations

import platform
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from api.auth import require_auth, token_matches
from api.errors import APIError
from api.services import Services
from api.services.overview import build_overview

router = APIRouter()


class LoginRequest(BaseModel):
    token: str = ""


def _services(request: Request) -> Services:
    return request.app.state.services


@router.get("/health")
def health(request: Request) -> dict:
    services = _services(request)
    checks: dict[str, dict] = {}

    try:
        checks["database"] = {"ok": bool(services.store.ping())}
    except Exception as exc:                            # noqa: BLE001
        checks["database"] = {"ok": False, "detail": str(exc)}

    try:
        mt5_health = services.mt5.health()
        if not isinstance(mt5_health, dict):
            mt5_health = {"ok": bool(mt5_health)}
        checks["mt5"] = mt5_health
    except Exception as exc:                            # noqa: BLE001
        checks["mt5"] = {"ok": False, "detail": str(exc)}

    try:
        checks["bridge"] = services.bridge_check()
    except Exception as exc:                            # noqa: BLE001
        checks["bridge"] = {"listening": False, "detail": str(exc)}

    try:
        checks["runner"] = {"ok": True, **services.runner.status()}
    except Exception as exc:                            # noqa: BLE001
        checks["runner"] = {"ok": False, "detail": str(exc)}

    checks["token"] = {"ok": bool(services.settings.dashboard_token)}
    checks["config"] = {
        "ok": services.settings.config_path.exists(),
        "path": str(services.settings.config_path),
    }

    degraded = (
        not checks["database"].get("ok")
        or not checks["mt5"].get("ok")
        or not checks["bridge"].get("listening")
        or not checks["token"].get("ok")
    )
    return {
        "status": "degraded" if degraded else "ok",
        "ts": services.clock(),
        "uptime_seconds": max(0.0, services.clock() - services.started_at),
        "checks": checks,
    }


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request) -> dict:
    services = _services(request)
    if not token_matches(services.settings.dashboard_token, payload.token.strip()):
        raise APIError(401, "unauthorized", "the dashboard token is not valid")
    return {"ok": True, "token_type": "bearer", "scope": "local"} 


@router.get("/auth/me", dependencies=[Depends(require_auth)])
def me(request: Request) -> dict:
    services = _services(request)
    return {
        "authenticated": True,
        "scope": "local",
        "mode": services.runner.status().get("mode"),
        "token_file": str(services.settings.token_file),
    }


@router.get("/overview", dependencies=[Depends(require_auth)])
def overview(request: Request) -> dict:
    return build_overview(_services(request))


@router.get("/system/info", dependencies=[Depends(require_auth)])
def system_info(request: Request) -> dict:
    services = _services(request)
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "root": str(services.settings.root),
        "config_path": str(services.settings.config_path),
        "database": str(services.settings.db_path),
        "token_file": str(services.settings.token_file),
        "bridge": {"host": services.settings.bridge_host,
                   "port": services.settings.bridge_port},
        "ws_interval": services.settings.ws_interval,
        "static_dir": str(services.settings.static_dir) if services.settings.static_dir else None,
        "time": time.time(),
    }


@router.get("/system/logs", dependencies=[Depends(require_auth)])
def system_logs(request: Request, lines: int = Query(80, ge=1, le=500)) -> dict:
    services = _services(request)
    files = []
    for path in services.settings.log_paths:
        entry = {"path": str(path), "available": Path(path).exists(), "lines": []}
        if entry["available"]:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    entry["lines"] = [line.rstrip("\r\n") for line in handle.readlines()[-lines:]]
            except OSError as exc:
                entry["available"] = False
                entry["reason"] = str(exc)
        files.append(entry)
    return {"files": files, "requested_lines": lines}
