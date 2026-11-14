"""Strategy Desk: configuration and research-only overrides.

The five engines are not on the live path yet.  This service exposes their
merged defaults and writes explicit research overrides to
``state/strategy_overrides.json``.  It never claims that a toggle changes the
legacy runner.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

from api.errors import APIError
from strategies.registry import STRATEGY_CLASSES, strategy_config

_DESCRIPTIONS = {
    "trend": "EMA fast/slow crossover with slope quality",
    "breakout": "Donchian range breakout with ATR confirmation",
    "smc": "SMC/ICT structure shifts, order blocks and fair value gaps",
    "momentum": "RSI bands plus short-term rate of change",
    "mean_reversion": "ATR stretch back toward the slow EMA",
}

_ALLOWED_PARAMS = {
    "enabled", "min_confidence", "min_rr", "max_signals_per_bar",
    "allow_opposite_directions", "weight", "lookback", "atr_mult",
    "risk_mult", "rsi_period", "ema_period", "swing_lookback",
}


class StrategiesService:
    def __init__(self, root: str | Path, config: Any, store: Any,
                 clock: Callable[[], float] | None = None):
        self.root = Path(root)
        self.config = config
        self.store = store
        self.overrides_path = self.root / "state" / "strategy_overrides.json"
        self._lock = threading.RLock()

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
            rows.append({
                "name": name,
                "description": _DESCRIPTIONS.get(name, name),
                "enabled": bool(override.get("enabled", engine_cfg.get("enabled", True))),
                "configured_enabled": bool(engine_cfg.get("enabled", True)),
                "weight": float(override.get("weight", weights.get(name, 0.0)) or 0.0),
                "params": engine_cfg,
                "overrides": override,
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
        clean: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if key not in _ALLOWED_PARAMS:
                raise APIError(422, "validation_error",
                               f"parameter {key!r} is not editable",
                               {"allowed": sorted(_ALLOWED_PARAMS)})
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,31}", str(key)):
                raise APIError(422, "validation_error", "invalid parameter name")
            if isinstance(value, bool):
                clean[key] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = float(value)
            elif isinstance(value, str) and len(value) <= 64:
                clean[key] = value
            else:
                raise APIError(422, "validation_error",
                               f"parameter {key!r} must be a number, boolean or short string")
        with self._lock:
            data = self._overrides()
            current = dict(data.get(name) or {})
            if enabled is not None:
                current["enabled"] = enabled
            if clean:
                current["params"] = {**(current.get("params") or {}), **clean}
            data[name] = current
            self._write_overrides(data)
        self.store.audit("local", "strategies.update", target=name,
                         payload={"enabled": enabled, "params": clean},
                         detail="research override")
        return self._one(name)

    def _one(self, name: str) -> dict:
        row = next((item for item in self.list()["strategies"]
                    if item["name"] == name), None)
        return row or {"name": name}


__all__ = ["StrategiesService"]
