"""Mutating controls: runner, risk, settings and research strategy overrides."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from api.auth import require_auth, require_mutation, run_idempotent
from api.errors import APIError
from api.services import Services

router = APIRouter()


def _services(request: Request) -> Services:
    return request.app.state.services


class RunnerStart(BaseModel):
    mode: str = "dry-run"
    confirm: str = ""


class EmergencyClose(BaseModel):
    dry_run: bool = True
    confirm: str = ""
    reason: str = ""


class StrategyUpdate(BaseModel):
    enabled: bool | None = None
    params: dict[str, Any] | None = None


class StrategyScan(BaseModel):
    symbol: str | None = None
    timeframe: str = "M15"
    bars: int = Field(400, ge=260, le=1500)


class StrategyReplay(BaseModel):
    symbol: str | None = None
    timeframe: str = "M15"
    bars: int = Field(1500, ge=320, le=1500)


class BacktestStart(BaseModel):
    symbol: str | None = None
    bars: int = Field(12000, ge=3000, le=30000)
    folds: int = Field(4, ge=1, le=8)
    holdout: float = Field(0.25, ge=0.05, le=0.5)
    min_test_bars: int = Field(200, ge=50, le=1000)
    mc_runs: int = Field(2000, ge=0, le=5000)
    seed: int = Field(42, ge=1, le=2_147_483_647)
    only: str | None = None
    no_sensitivity: bool = False


@router.get("/runner", dependencies=[Depends(require_auth)])
def runner_status(request: Request) -> dict:
    return {"runner": _services(request).runner.status()}


@router.post("/runner/start", dependencies=[Depends(require_auth),
                                             Depends(require_mutation)])
def runner_start(payload: RunnerStart, request: Request) -> dict:
    services = _services(request)
    mode = (payload.mode or "dry-run").strip().lower()
    if mode in {"live", "real"}:
        raise APIError(
            403, "live_disabled",
            "live runner start is disabled: the walk-forward gate failed and "
            "no engine has been promoted",
            {"mode": "live", "allowed": ["dry-run"]})
    if mode not in {"dry", "dry-run", "paper"}:
        raise APIError(422, "validation_error",
                       "mode must be dry-run (paper) or live")

    def action() -> dict:
        status = services.runner.start()
        services.store.audit("local", "runner.start", target="runner",
                             payload={"mode": "dry-run"},
                             detail=f"pid={status.get('pid')}")
        return {"runner": status, "mode": "dry-run"}

    return run_idempotent(request, "runner.start", action)


@router.post("/runner/stop", dependencies=[Depends(require_auth),
                                            Depends(require_mutation)])
def runner_stop(request: Request) -> dict:
    services = _services(request)
    status = services.runner.stop()
    services.store.audit("local", "runner.stop", target="runner",
                         detail=f"pid={status.get('stopped_pid') or status.get('pid')}")
    return {"runner": status}


@router.get("/risk", dependencies=[Depends(require_auth)])
def risk(request: Request) -> dict:
    return _services(request).risk.snapshot()


@router.post("/risk/emergency-close", dependencies=[Depends(require_auth),
                                                     Depends(require_mutation)])
def emergency_close(payload: EmergencyClose, request: Request) -> dict:
    services = _services(request)

    def action() -> dict:
        return services.risk.emergency_close(
            dry_run=payload.dry_run, confirm=payload.confirm)

    return run_idempotent(request, "risk.emergency_close", action)


@router.post("/risk/reset-guardian", dependencies=[Depends(require_auth),
                                                    Depends(require_mutation)])
def reset_guardian(request: Request) -> dict:
    return _services(request).risk.reset_guardian()


@router.get("/settings", dependencies=[Depends(require_auth)])
def get_settings(request: Request) -> dict:
    return _services(request).config.public()


@router.patch("/settings", dependencies=[Depends(require_auth),
                                          Depends(require_mutation)])
def patch_settings(payload: dict[str, Any], request: Request) -> dict:
    services = _services(request)
    current = services.runner.status()
    if current.get("mode") not in (None, "stopped"):
        raise APIError(
            409, "runner_active",
            "settings cannot be changed while the runner is active; stop the "
            "runner, apply the change, then start it again",
            {"runner": current.get("mode")})
    return services.config.update(payload)


@router.get("/strategies", dependencies=[Depends(require_auth)])
def list_strategies(request: Request) -> dict:
    return _services(request).strategies.list()


@router.patch("/strategies/{name}", dependencies=[Depends(require_auth),
                                                   Depends(require_mutation)])
def update_strategy(name: str, payload: StrategyUpdate, request: Request) -> dict:
    result = _services(request).strategies.update(
        name, enabled=payload.enabled, params=payload.params)
    return {"strategy": result}


@router.post("/strategies/signals", dependencies=[Depends(require_auth)])
def strategy_signals(payload: StrategyScan, request: Request) -> dict:
    return _services(request).strategies.signals(
        symbol=payload.symbol, timeframe=payload.timeframe, bars=payload.bars)


@router.post("/strategies/replay", dependencies=[Depends(require_auth)])
def strategy_replay(payload: StrategyReplay, request: Request) -> dict:
    return _services(request).strategies.replay(
        symbol=payload.symbol, timeframe=payload.timeframe, bars=payload.bars)


@router.post("/backtests", dependencies=[Depends(require_auth),
                                          Depends(require_mutation)])
def start_backtest(payload: BacktestStart, request: Request) -> dict:
    services = _services(request)
    params = {
        "symbol": payload.symbol,
        "bars": payload.bars,
        "folds": payload.folds,
        "holdout": payload.holdout,
        "min_test_bars": payload.min_test_bars,
        "mc_runs": payload.mc_runs,
        "seed": payload.seed,
        "only": payload.only,
        "no_sensitivity": payload.no_sensitivity,
    }

    def action() -> dict:
        job = services.backtests.start(params)
        return {"job": job}

    return run_idempotent(request, "backtest.start", action)
