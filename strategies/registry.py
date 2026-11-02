"""Strategy registry and configuration merge.

The five engines are independent: each has its own defaults, its own
enable/disable switch, and its own parameter block.  The registry is the only
place that knows the class list, so the runner, replay harness and tests all
build the same objects.
"""
from __future__ import annotations

from strategies.base import BaseStrategy
from strategies.breakout import BreakoutStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumStrategy
from strategies.smc import SmcStrategy
from strategies.trend import TrendStrategy

STRATEGY_CLASSES: dict[str, type[BaseStrategy]] = {
    "trend": TrendStrategy,
    "breakout": BreakoutStrategy,
    "smc": SmcStrategy,
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
}

DEFAULT_WEIGHTS = {
    "trend": 1.10,
    "breakout": 1.00,
    "smc": 1.15,
    "momentum": 0.95,
    "mean_reversion": 0.80,
}

DEFAULT_STRATEGY_CONFIG = {
    "enabled": True,
    "min_confidence": 0.45,
    "max_signals_per_bar": 2,
    "allow_opposite_directions": False,
    "min_rr": 0.8,
    "weights": dict(DEFAULT_WEIGHTS),
    "engines": {
        name: {"enabled": True}
        for name in STRATEGY_CLASSES
    },
}


def strategy_config(config: dict | None) -> dict:
    """Return a fully-populated strategy section without mutating ``config``."""
    section = dict((config or {}).get("strategies") or {})
    merged = {**DEFAULT_STRATEGY_CONFIG, **section}
    merged["weights"] = {**DEFAULT_WEIGHTS, **(section.get("weights") or {})}
    merged["engines"] = {
        name: {**(DEFAULT_STRATEGY_CONFIG["engines"].get(name) or {}),
               **((section.get("engines") or {}).get(name) or {})}
        for name in STRATEGY_CLASSES
    }
    return merged


def build_strategies(config: dict | None) -> dict[str, BaseStrategy]:
    """Build every configured engine, including disabled ones.

    Disabled engines are returned so the dashboard and report can show them
    explicitly; their ``evaluate`` always returns an empty list.
    """
    section = strategy_config(config)
    engines = section.get("engines") or {}
    master_on = bool(section.get("enabled", True))
    out: dict[str, BaseStrategy] = {}
    for name, cls in STRATEGY_CLASSES.items():
        params = {**cls.defaults, **(engines.get(name) or {})}
        if not master_on:
            params["enabled"] = False
        out[name] = cls(params)
    return out


def engine_weights(config: dict | None) -> dict[str, float]:
    return dict(strategy_config(config).get("weights") or DEFAULT_WEIGHTS)


__all__ = [
    "STRATEGY_CLASSES",
    "DEFAULT_WEIGHTS",
    "DEFAULT_STRATEGY_CONFIG",
    "build_strategies",
    "engine_weights",
    "strategy_config",
]
