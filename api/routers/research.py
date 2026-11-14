"""Market, committee, learning and backtest read surfaces."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from api.auth import require_auth
from api.services import Services

router = APIRouter(dependencies=[Depends(require_auth)])


def _services(request: Request) -> Services:
    return request.app.state.services


@router.get("/market/quote")
def quote(request: Request, symbol: str = Query(..., min_length=1)) -> dict:
    return _services(request).market.quote(symbol)


@router.get("/market/candles")
def candles(request: Request, symbol: str = Query(..., min_length=1),
            timeframe: str = "M15", bars: int = Query(120, ge=1, le=1500)) -> dict:
    return _services(request).market.candles(symbol, timeframe=timeframe, bars=bars)


@router.get("/market/overview")
def market_overview(request: Request, symbol: str = Query(..., min_length=1)) -> dict:
    return _services(request).market.overview(symbol)


@router.get("/committee")
def committee(request: Request) -> dict:
    return _services(request).committee.snapshot()


@router.get("/learning/summary")
def learning_summary(request: Request) -> dict:
    return _services(request).learning.summary()


@router.get("/learning/trades")
def learning_trades(request: Request, limit: int = Query(100, ge=1, le=1000)) -> dict:
    return _services(request).learning.trades(limit=limit)


@router.get("/backtests")
def backtests(request: Request) -> dict:
    return _services(request).backtests.list()
