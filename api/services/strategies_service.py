"""Strategy Desk: research overrides, current-bar signals and replay.

The five engines are not on the live path yet.  This service exposes their
merged defaults, writes explicit research overrides to
``state/strategy_overrides.json``, and runs the same deterministic engines used
by the replay harness.  It never claims that a toggle changes the legacy
runner.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from api.errors import APIError
from market.data_engine import timeframe_seconds
from market.features import build_features
from strategies.arbiter import StrategyArbiter
from strategies.base import StrategyContext
from strategies.registry import STRATEGY_CLASSES, build_strategies, strategy_config
from strategies.replay import ReplayConfig, run_replay

_DESCRIPTIONS = {
    "trend": "EMA fast/slow crossover with slope quality",
    "breakout": "Donchian range breakout with ATR confirmation",
    "smc": "SMC/ICT structure shifts, order blocks and fair value gaps",
    "momentum": "RSI bands plus short-term rate of change",
    "mean_reversion": "ATR stretch back toward the slow EMA",
}

_GLOBAL_SECTION_PARAMS = {
    "min_confidence", "min_rr", "max_signals_per_bar",
    "allow_opposite_directions",
}

_TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}


def _editable_params(name: str) -> set[str]:
    """Numeric engine parameters are safe to override; lists/objects are not."""
    defaults = STRATEGY_CLASSES[name].defaults
    return {key for key, value in defaults.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)}


def _clean(value: Any) -> Any:
    """Replace JSON-hostile non-finite floats while keeping the shape."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


class StrategiesService:
    def __init__(self, root: str | Path, config: Any, store: Any,
                 mt5: Any | None = None,
                 clock: Callable[[], float] | None = None):
        self.root = Path(root)
        self.config = config
        self.store = store
        self.mt5 = mt5
        self.clock = clock or time.time
        self.overrides_path = self.root / "state" / "strategy_overrides.json"
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def _overrides(self) -> dict:
        if not self.overrides_path.exists():
            return {}
        try:
            data = json.loads(self.overrides_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_overrides(self, data: dict) -> None:
        self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.overrides_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.overrides_path)

    def _effective_config(self) -> dict:
        """Merge research overrides into a copy of the loaded config."""
        cfg = copy.deepcopy(self.config.raw() or {})
        if not isinstance(cfg.get("strategies"), dict):
            cfg["strategies"] = {}
        if not isinstance(cfg["strategies"].get("engines"), dict):
            cfg["strategies"]["engines"] = {}
        engines = cfg["strategies"]["engines"]
        for name, override in self._overrides().items():
            if name not in STRATEGY_CLASSES or not isinstance(override, dict):
                continue
            engine = engines.setdefault(name, {})
            if "enabled" in override:
                engine["enabled"] = bool(override["enabled"])
            if "weight" in override:
                weights = cfg["strategies"].setdefault("weights", {})
                weights[name] = float(override["weight"])
            params = override.get("params")
            if isinstance(params, dict):
                engine.update(params)
        return cfg

    def _trading_defaults(self) -> tuple[str, str]:
        trading = (self.config.raw() or {}).get("trading") or {}
        return (str(trading.get("symbol") or "XAUUSD"),
                str(trading.get("timeframe") or "M15").upper())

    def _closed_candles(self, symbol: str, timeframe: str,
                        bars: int) -> tuple[list[dict], float]:
        if self.mt5 is None:
            raise APIError(503, "mt5_unavailable",
                           "MT5 is not wired into the strategy service")
        timeframe = str(timeframe or "M15").upper()
        if timeframe not in _TIMEFRAMES:
            raise APIError(422, "validation_error",
                           f"unsupported timeframe {timeframe!r}")
        rows = self.mt5.candles(symbol, timeframe, max(2, int(bars)))
        if len(rows) < 2:
            raise APIError(503, "no_market_data",
                           f"MT5 returned {len(rows)} candle(s) for {symbol}")
        # copy_rates_from_pos includes the forming bar.  Every research
        # calculation here is close-of-bar only, so that bar is discarded.
        closed = list(rows[:-1])
        return closed, float(rows[-1].get("time") or 0.0)

    def _quote(self, symbol: str) -> tuple[dict | None, str]:
        if self.mt5 is None:
            return None, "MT5 unavailable"
        try:
            quote = self.mt5.quote(symbol)
            return dict(quote), "live quote"
        except Exception as exc:                            # noqa: BLE001
            return None, f"quote unavailable: {exc}"

    # -------------------------------------------------------------------- read
    def list(self) -> dict:
        cfg = self.config.raw()
        section = strategy_config(cfg)
        overrides = self._overrides()
        engines = section.get("engines") or {}
        weights = section.get("weights") or {}
        rows = []
        for name in STRATEGY_CLASSES:
            engine_cfg = dict(engines.get(name) or {})
            override = dict(overrides.get(name) or {})
            override_params = dict(override.get("params") or {})
            merged_params = {
                **STRATEGY_CLASSES[name].defaults,
                **engine_cfg,
                **override_params,
            }
            rows.append({
                "name": name,
                "description": _DESCRIPTIONS.get(name, name),
                "enabled": bool(override.get("enabled",
                                             override_params.get("enabled",
                                                                 engine_cfg.get("enabled", True)))),
                "configured_enabled": bool(engine_cfg.get("enabled", True)),
                "weight": float(override.get("weight", weights.get(name, 0.0)) or 0.0),
                "params": _clean(merged_params),
                "overrides": override,
                "editable_params": sorted(_editable_params(name)),
                "research_only": True,
            })
        return {
            "strategies": rows,
            "research_only": True,
            "live_path": "legacy consensus runner; these engines are replay-only",
            "section": {
                "enabled": section.get("enabled"),
                "min_confidence": section.get("min_confidence"),
                "min_rr": section.get("min_rr"),
                "max_signals_per_bar": section.get("max_signals_per_bar"),
                "allow_opposite_directions": section.get("allow_opposite_directions"),
            },
            "validated": False,
            "validation_note": ("walk-forward gate failed on 2026-11-12; "
                                "no engine is promoted"),
        }

    # ------------------------------------------------------------------- write
    def update(self, name: str, *, enabled: bool | None = None,
               params: dict | None = None) -> dict:
        name = str(name or "").strip().lower()
        if name not in STRATEGY_CLASSES:
            raise APIError(404, "unknown_strategy", f"unknown strategy {name!r}")
        if enabled is not None and not isinstance(enabled, bool):
            raise APIError(422, "validation_error", "enabled must be true/false")
        if params is not None and not isinstance(params, dict):
            raise APIError(422, "validation_error", "params must be an object")
        if params and _GLOBAL_SECTION_PARAMS.intersection(params):
            raise APIError(
                422, "validation_error",
                "section-wide parameters are not editable from a single engine",
                {"fields": sorted(_GLOBAL_SECTION_PARAMS)})
        allowed = _editable_params(name)
        clean: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if key == "enabled":
                if not isinstance(value, bool):
                    raise APIError(422, "validation_error",
                                   "enabled must be true/false")
                if enabled is None:
                    enabled = value
                continue
            if key == "weight":
                weight = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
                if weight is None or not math.isfinite(weight) or not (0.0 <= weight <= 10.0):
                    raise APIError(422, "validation_error",
                                   "weight must be a number between 0 and 10")
                continue
            if key not in allowed:
                raise APIError(422, "validation_error",
                               f"parameter {key!r} is not editable",
                               {"allowed": sorted(allowed | {"enabled", "weight"})})
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,31}", str(key)):
                raise APIError(422, "validation_error", "invalid parameter name")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise APIError(422, "validation_error",
                               f"parameter {key!r} must be a number")
            number = float(value)
            if not math.isfinite(number) or not (0.0 < number <= 10_000.0):
                raise APIError(422, "validation_error",
                               f"parameter {key!r} must be finite and between 0 and 10000")
            clean[key] = number
        clean_weight = None
        if params and "weight" in params:
            clean_weight = float(params["weight"])
        with self._lock:
            data = self._overrides()
            current = dict(data.get(name) or {})
            if enabled is not None:
                current["enabled"] = enabled
            if clean_weight is not None:
                current["weight"] = clean_weight
            if clean:
                current["params"] = {**(current.get("params") or {}), **clean}
            data[name] = current
            self._write_overrides(data)
        self.store.audit("local", "strategies.update", target=name,
                         payload={"enabled": enabled, "weight": clean_weight,
                                  "params": clean},
                         detail="research override")
        return self._one(name)

    # ------------------------------------------------------------------ signals
    def signals(self, symbol: str | None = None, timeframe: str | None = None,
                bars: int = 400) -> dict:
        default_symbol, default_timeframe = self._trading_defaults()
        symbol = str(symbol or default_symbol).strip().upper()
        timeframe = str(timeframe or default_timeframe).upper()
        if not symbol:
            raise APIError(422, "validation_error", "symbol is required")
        cfg = self._effective_config()
        closed, forming_time = self._closed_candles(symbol, timeframe, bars)
        if len(closed) < 260:
            raise APIError(
                422, "insufficient_data",
                f"need at least 260 closed bars for features; got {len(closed)}",
                {"bars": len(closed), "timeframe": timeframe})
        try:
            frame = build_features(
                pd.DataFrame(closed),
                (cfg.get("market") or {}).get("features"))
        except Exception as exc:                            # noqa: BLE001
            raise APIError(422, "feature_error",
                           f"could not build features: {exc}") from exc
        quote, spread_source = self._quote(symbol)
        spread = float(quote.get("spread_points") or 0.0) if quote else None
        risk_cfg = cfg.get("risk") or {}
        point = float((quote or {}).get("point") or risk_cfg.get("point") or 0.01)
        digits = int((quote or {}).get("digits") or risk_cfg.get("digits") or 2)
        engine_map = build_strategies(cfg)
        ctx = StrategyContext(
            symbol=symbol,
            timeframe=timeframe,
            frame=frame,
            point=point,
            digits=digits,
            spread_points=float(spread or 0.0),
            index=len(frame) - 1,
            config=cfg,
        )
        proposals = []
        engines = []
        for name, engine in engine_map.items():
            produced = list(engine.evaluate(ctx)) if engine.enabled else []
            engines.append({"name": name, "enabled": bool(engine.enabled),
                            "proposals": len(produced)})
            proposals.extend(produced)
        arbiter = StrategyArbiter(cfg)
        arbitration = arbiter.arbitrate(
            proposals, now=ctx.bar_time + timeframe_seconds(timeframe))
        selected_ids = {(signal.strategy, signal.setup_id)
                        for signal in arbitration.selected}
        rejected = {(item.signal.strategy, item.signal.setup_id): item.reason
                    for item in arbitration.rejected}
        rows = []
        for signal in proposals:
            key = (signal.strategy, signal.setup_id)
            row = signal.as_dict()
            row["status"] = "selected" if key in selected_ids else "rejected"
            row["reject_reason"] = rejected.get(key)
            row["score"] = round(arbiter.score(signal), 4)
            rows.append(row)
        rows.sort(key=lambda row: (row["status"] != "selected", -row["score"]))
        return _clean({
            "available": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "generated_at": self.clock(),
            "last_closed_bar": ctx.bar_time,
            "forming_bar_time": forming_time,
            "bars": len(closed),
            "closed_bars_only": True,
            "spread_points": spread,
            "spread_source": spread_source,
            "spread_assumed_zero": spread is None,
            "engines": engines,
            "proposals": rows,
            "selected": [signal.as_dict() for signal in arbitration.selected],
            "rejections": arbitration.rejections_by_reason,
            "config_source": ("config.json + state/strategy_overrides.json"
                              if self._overrides() else "config.json defaults"),
            "research_only": True,
        })

    # ------------------------------------------------------------------- replay
    def replay(self, symbol: str | None = None, timeframe: str | None = None,
               bars: int = 1500) -> dict:
        default_symbol, default_timeframe = self._trading_defaults()
        symbol = str(symbol or default_symbol).strip().upper()
        timeframe = str(timeframe or default_timeframe).upper()
        if not symbol:
            raise APIError(422, "validation_error", "symbol is required")
        cfg = self._effective_config()
        closed, forming_time = self._closed_candles(symbol, timeframe, bars)
        if len(closed) < 320:
            raise APIError(
                422, "insufficient_data",
                f"need at least 320 closed bars for a replay; got {len(closed)}",
                {"bars": len(closed), "timeframe": timeframe})
        quote, spread_source = self._quote(symbol)
        spread = float(quote.get("spread_points") or 0.0) if quote else None
        overrides: dict[str, Any] = {
            "symbol": symbol,
            "timeframe": timeframe,
        }
        if quote:
            overrides["point"] = float(quote.get("point") or 0.01)
            overrides["digits"] = int(quote.get("digits") or 2)
        if spread is not None:
            overrides["spread_points"] = spread
        replay_cfg = ReplayConfig.from_config(cfg, **overrides)
        with self._run_lock:
            try:
                result = run_replay(
                    pd.DataFrame(closed), config=cfg, replay=replay_cfg,
                    data_note=(f"MT5 {symbol} {timeframe}: {len(closed)} closed "
                               "bars; in-sample, next-bar fills"))
            except Exception as exc:                        # noqa: BLE001
                raise APIError(422, "replay_error",
                               f"replay failed: {exc}") from exc
        self.store.audit(
            "local", "strategies.replay", target=symbol,
            payload={"timeframe": timeframe, "bars": len(closed)},
            detail=f"trades={len(result.trades)}")
        trades = [trade.as_dict() for trade in result.trades[-300:]]
        return _clean({
            "available": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "generated_at": self.clock(),
            "last_closed_bar": float(closed[-1].get("time") or 0.0),
            "forming_bar_time": forming_time,
            "bars": result.bars,
            "closed_bars_only": True,
            "data_note": result.data_note,
            "spread_points": replay_cfg.spread_points,
            "slippage_points": replay_cfg.slippage_points,
            "spread_source": spread_source,
            "proposals": result.proposals,
            "selected": result.selected,
            "rejections": result.rejections,
            "combined": result.metrics(),
            "by_strategy": result.by_strategy(),
            "by_session": result.by_session(),
            "equity_curve": result.equity_curve(),
            "trades": trades,
            "trades_total": len(result.trades),
            "research_only": True,
            "validation": {
                "walkforward_passed": False,
                "note": ("in-sample replay only; the 2026-11-12 walk-forward "
                         "gate failed, so no engine is promoted"),
            },
        })

    def _one(self, name: str) -> dict:
        row = next((item for item in self.list()["strategies"]
                    if item["name"] == name), None)
        return row or {"name": name}


__all__ = ["StrategiesService"]
