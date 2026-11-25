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
def market_overview(request: Request, symbol: str = Query(..., min_length=1),
                    timeframe: str = "M15") -> dict:
    return _services(request).market.overview(symbol, timeframe=timeframe)


@router.get("/committee")
def committee(request: Request) -> dict:
    services = _services(request)
    payload = services.committee.snapshot()
    try:
        rows = services.journal.decisions(limit=40)
    except Exception as exc:                            # noqa: BLE001
        payload["reviews"] = []
        payload["reviews_available"] = False
        payload["reviews_reason"] = f"decision journal unavailable: {exc}"
        return payload
    reviews = []
    for row in rows:
        action = str(row.get("llm_action") or "").strip().lower()
        comment = str(row.get("llm_comment") or "").strip()
        if action in ("", "n/a", "none", "skipped") and not comment:
            continue
        reviews.append({
            "ts": row.get("ts"),
            "symbol": row.get("symbol"),
            "action": row.get("action"),
            "direction": row.get("direction"),
            "volume": row.get("volume"),
            "llm_action": row.get("llm_action"),
            "llm_comment": comment,
            "reason": row.get("reason"),
            "executed": row.get("executed"),
        })
    payload["reviews"] = reviews
    payload["reviews_available"] = bool(getattr(services.journal, "available", True))
    if not reviews:
        payload["reviews_reason"] = "no LLM review has been recorded in the journal"
    return payload


@router.get("/funnel")
def funnel(request: Request, hours: int = Query(24, ge=1, le=720),
           symbol: str | None = Query(None, min_length=1)) -> dict:
    """Real-data conversion funnel: signals → agents → Kronos → Gemini → risk
    → execution → closed trades, with the recorded rejection reasons."""
    services = _services(request)
    service = services.funnel
    if service is None:                 # tests / older wiring: build on demand
        from api.services.funnel_service import FunnelService
        service = FunnelService(services.journal, services.mt5)
    return service.snapshot(hours=hours, symbol=symbol)


@router.get("/learning/summary")
def learning_summary(request: Request) -> dict:
    return _services(request).learning.summary()


@router.get("/learning/trades")
def learning_trades(request: Request, limit: int = Query(100, ge=1, le=1000)) -> dict:
    return _services(request).learning.trades(limit=limit)


@router.get("/backtests")
def backtests(request: Request) -> dict:
    return _services(request).backtests.list()


@router.get("/backtests/{job_id}")
def backtest_job(job_id: str, request: Request) -> dict:
    return {"job": _services(request).backtests.get(job_id)}


@router.get("/backtests/{job_id}/report")
def backtest_report(job_id: str, request: Request) -> dict:
    return _services(request).backtests.report(job_id)
