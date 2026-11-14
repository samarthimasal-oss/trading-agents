"""FastAPI application for the local TradingAgents control room."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.errors import APIError, error_body
from api.factory import build_services
from api.routers import account, control, research, system
from api.services import ApiSettings, Services
from api.ws import router as ws_router

log = logging.getLogger("api")


def _static_candidate(static_root: Path, full_path: str) -> Optional[Path]:
    """Resolve a requested file, refusing to escape the built SPA directory."""
    if not full_path or "\x00" in full_path:
        return None
    root = static_root.resolve()
    try:
        candidate = (root / full_path).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def _validation_detail(exc: RequestValidationError) -> list[dict]:
    detail = []
    for item in exc.errors():
        detail.append({
            "loc": [str(part) for part in item.get("loc", [])],
            "msg": item.get("msg", "invalid value"),
            "type": item.get("type", "validation_error"),
        })
    return detail


def create_app(*, settings: Optional[ApiSettings] = None,
               services: Optional[Services] = None) -> FastAPI:
    services = services or build_services(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        for closer in reversed(services.closeables):
            try:
                closer()
            except Exception:                            # noqa: BLE001
                log.debug("service close failed", exc_info=True)

    app = FastAPI(
        title="TradingAgents dashboard",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.services = services

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Dashboard-Request",
                       "Idempotency-Key"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # ------------------------------------------------------------------ errors
    @app.exception_handler(APIError)
    async def api_error_handler(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request,
                                       exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body("validation_error", "request validation failed",
                               _validation_detail(exc)))

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request,
                                 exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed",
                403: "forbidden"}.get(exc.status_code, "http_error")
        return JSONResponse(status_code=exc.status_code,
                            content=error_body(code, str(exc.detail)))

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled API error")
        return JSONResponse(status_code=500,
                            content=error_body("internal_error",
                                               "an unexpected server error occurred",
                                               str(exc) if log.isEnabledFor(logging.DEBUG) else None))

    # ----------------------------------------------------------------- routers
    for router in (system.router, account.router, control.router,
                   research.router, ws_router):
        app.include_router(router, prefix="/api/v1")

    # ------------------------------------------------------------- React build
    static_dir = Path(services.settings.static_dir).resolve() if services.settings.static_dir else None
    index = static_dir / "index.html" if static_dir else None
    if static_dir and index and index.exists():
        assets = static_dir / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            if full_path.startswith("api/"):
                raise StarletteHTTPException(status_code=404, detail="Not Found")
            candidate = _static_candidate(static_dir, full_path)
            if candidate is not None:
                return FileResponse(candidate)
            if full_path and "." in Path(full_path).name:
                raise StarletteHTTPException(status_code=404, detail="Not Found")
            return FileResponse(index)

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8766, log_level="info")


if __name__ == "__main__":
    main()


__all__ = ["create_app"]
