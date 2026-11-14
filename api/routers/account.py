"""Broker account, open orders and the runner journal."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from api.auth import require_auth
from api.services import Services

router = APIRouter(dependencies=[Depends(require_auth)])


def _services(request: Request) -> Services:
    return request.app.state.services


def _magic(request: Request) -> int | None:
    try:
        value = (_services(request).config.raw().get("trading") or {}).get("magic")
        return int(value) if value is not None else None
    except Exception:                                   # noqa: BLE001
        return None


@router.get("/account")
def account(request: Request) -> dict:
    return _services(request).mt5.account()


@router.get("/positions")
def positions(request: Request, symbol: str | None = None,
              magic: int | None = Query(None)) -> dict:
    services = _services(request)
    rows = services.mt5.positions(symbol=symbol, magic=magic if magic is not None else _magic(request))
    return {"positions": rows, "count": len(rows)}


@router.get("/orders")
def orders(request: Request, symbol: str | None = None,
           magic: int | None = Query(None)) -> dict:
    services = _services(request)
    rows = services.mt5.orders(symbol=symbol, magic=magic if magic is not None else _magic(request))
    return {"orders": rows, "count": len(rows)}


@router.get("/decisions")
def decisions(request: Request, limit: int = Query(100, ge=1, le=1000),
              symbol: str | None = None,
              since: float | None = Query(None)) -> dict:
    rows = _services(request).journal.decisions(limit=limit, symbol=symbol, since=since)
    return {"decisions": rows, "count": len(rows)}


@router.get("/events")
def events(request: Request, limit: int = Query(100, ge=1, le=1000),
           kind: str | None = None, symbol: str | None = None) -> dict:
    rows = _services(request).journal.events(limit=limit, kind=kind, symbol=symbol)
    return {"events": rows, "count": len(rows)}


@router.get("/equity")
def equity(request: Request, limit: int = Query(500, ge=1, le=5000)) -> dict:
    rows = _services(request).journal.equity(limit=limit)
    return {"equity": rows, "count": len(rows)}
