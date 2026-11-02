"""Startup configuration validation.

The runner used to read ``config.json`` with direct key indexing, so a typo or
a missing value surfaced as a ``KeyError`` deep inside a trading cycle.  This
module fails **fast and loudly** at startup with a list of every problem, and
applies only safe defaults for optional sections.

It deliberately does not require an LLM key: the LLM is optional and the
system must keep running without one.  Secret *values* are never logged.
"""
from __future__ import annotations

import math
from typing import Any

TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"}


class ConfigError(ValueError):
    """Raised when ``config.json`` is structurally or numerically invalid."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors))


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _bool(v: Any) -> bool:
    return isinstance(v, bool)


def _str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _section(cfg: dict, name: str, errors: list[str]) -> dict:
    value = cfg.get(name)
    if not isinstance(value, dict):
        errors.append(f"{name}: must be an object")
        return {}
    return value


def _number(errors: list[str], section: dict, path: str, key: str, *,
            lo: float | None = None, hi: float | None = None,
            required: bool = True, default: float | None = None) -> None:
    value = section.get(key, default)
    if value is None:
        if required:
            errors.append(f"{path}.{key}: missing")
        return
    if not _num(value):
        errors.append(f"{path}.{key}: must be a number")
        return
    if lo is not None and value < lo:
        errors.append(f"{path}.{key}: {value} < minimum {lo}")
    if hi is not None and value > hi:
        errors.append(f"{path}.{key}: {value} > maximum {hi}")


def _integer(errors: list[str], section: dict, path: str, key: str, *,
             lo: int | None = None, hi: int | None = None,
             required: bool = True) -> None:
    value = section.get(key)
    if value is None:
        if required:
            errors.append(f"{path}.{key}: missing")
        return
    if not _int(value):
        errors.append(f"{path}.{key}: must be an integer")
        return
    if lo is not None and value < lo:
        errors.append(f"{path}.{key}: {value} < minimum {lo}")
    if hi is not None and value > hi:
        errors.append(f"{path}.{key}: {value} > maximum {hi}")


def _boolean(errors: list[str], section: dict, path: str, key: str,
             *, required: bool = False) -> None:
    if key not in section:
        if required:
            errors.append(f"{path}.{key}: missing")
        return
    if not _bool(section[key]):
        errors.append(f"{path}.{key}: must be true/false")


def _string(errors: list[str], section: dict, path: str, key: str,
            *, required: bool = True) -> None:
    if key not in section or section.get(key) in (None, ""):
        if required:
            errors.append(f"{path}.{key}: missing")
        return
    if not isinstance(section[key], str):
        errors.append(f"{path}.{key}: must be a string")


def validate_config(cfg: dict) -> dict:
    """Validate and lightly normalise ``cfg``.  Returns the same object.

    Raises :class:`ConfigError` with every problem found, not just the first.
    """
    if not isinstance(cfg, dict):
        raise ConfigError(["top level: config must be a JSON object"])

    errors: list[str] = []

    # optional sections get empty defaults so downstream code can rely on them
    for name in ("kronos", "news", "telegram", "llm"):
        cfg.setdefault(name, {})

    mt5 = _section(cfg, "mt5", errors)
    _string(errors, mt5, "mt5", "terminal_path", required=False)
    _integer(errors, mt5, "mt5", "login", lo=0, required=False)
    _integer(errors, mt5, "mt5", "timeout_ms", lo=1000, hi=600000, required=False)
    _number(errors, mt5, "mt5", "deviation_points", lo=0, hi=10000, required=False)

    trading = _section(cfg, "trading", errors)
    symbols = trading.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        errors.append("trading.symbols: must be a non-empty list")
    elif not all(isinstance(s, str) and s.strip() for s in symbols):
        errors.append("trading.symbols: every entry must be a non-empty string")
    tf = trading.get("timeframe")
    if tf not in TIMEFRAMES:
        errors.append(f"trading.timeframe: {tf!r} is not one of {sorted(TIMEFRAMES)}")
    _integer(errors, trading, "trading", "magic", lo=1)
    _integer(errors, trading, "trading", "bars_history", lo=100, hi=100000)
    _number(errors, trading, "trading", "poll_seconds", lo=1, hi=3600)
    _boolean(errors, trading, "trading", "close_on_opposite")
    _boolean(errors, trading, "trading", "require_llm_approval")
    if not trading.get("symbol") and isinstance(symbols, list) and symbols:
        trading["symbol"] = symbols[0]
    if trading.get("symbol") and symbols and trading["symbol"] not in symbols:
        # not fatal at runtime (symbol resolution handles suffixes), but it is
        # almost certainly a configuration mistake
        errors.append("trading.symbol: must appear in trading.symbols")

    agents = _section(cfg, "agents", errors)
    for key in ("ema_fast", "ema_slow", "rsi_period", "atr_period", "breakout_lookback"):
        _integer(errors, agents, "agents", key, lo=2, hi=1000)
    for key in ("atr_sl_mult", "atr_tp_mult"):
        _number(errors, agents, "agents", key, lo=0.05, hi=50)
    for key in ("weight_trend", "weight_momentum", "weight_reversion",
                "weight_breakout", "weight_pullback", "weight_kronos"):
        _number(errors, agents, "agents", key, lo=0, hi=10, required=False)
    _number(errors, agents, "agents", "consensus_threshold", lo=0, hi=1)
    if _int(agents.get("ema_fast")) and _int(agents.get("ema_slow")):
        if agents["ema_fast"] >= agents["ema_slow"]:
            errors.append("agents.ema_fast: must be smaller than agents.ema_slow")

    risk = _section(cfg, "risk", errors)
    _number(errors, risk, "risk", "risk_percent_per_trade", lo=0, hi=5)
    _number(errors, risk, "risk", "fixed_lot", lo=0, hi=1000, required=False)
    _number(errors, risk, "risk", "max_notional_leverage_per_position", lo=0, hi=100, required=False)
    _integer(errors, risk, "risk", "max_positions", lo=1, hi=50)
    _integer(errors, risk, "risk", "max_open_same_symbol", lo=1, hi=20)
    _number(errors, risk, "risk", "max_daily_loss_percent", lo=0, hi=100)
    _number(errors, risk, "risk", "max_equity_drawdown_percent", lo=0, hi=100)
    _number(errors, risk, "risk", "max_spread_points", lo=0, hi=1_000_000)
    per_symbol_spreads = risk.get("max_spread_points_by_symbol")
    if per_symbol_spreads is not None:
        if not isinstance(per_symbol_spreads, dict):
            errors.append("risk.max_spread_points_by_symbol: must be an object")
        else:
            for sym, limit in per_symbol_spreads.items():
                if not isinstance(sym, str) or not sym.strip():
                    errors.append("risk.max_spread_points_by_symbol: keys must be symbol names")
                    continue
                if not _num(limit) or float(limit) < 0 or float(limit) > 1_000_000:
                    errors.append(
                        f"risk.max_spread_points_by_symbol.{sym}: must be a number between 0 and 1000000"
                    )
    for key in ("breakeven_atr", "trailing_atr", "trailing_step_atr",
                "dead_market_atr_ratio", "min_gap_seconds_between_same_symbol_trades"):
        _number(errors, risk, "risk", key, lo=0, hi=1_000_000, required=False)
    _number(errors, risk, "risk", "partial_close_at_r", lo=0.1, hi=100, required=False)
    _number(errors, risk, "risk", "partial_close_pct", lo=1, hi=100, required=False)
    _number(errors, risk, "risk", "time_stop_minutes", lo=0, hi=100000, required=False)
    _boolean(errors, risk, "risk", "partial_close_enabled")
    hours = risk.get("trading_hours_utc")
    if not isinstance(hours, list) or len(hours) != 2 or not all(_num(h) for h in hours):
        errors.append("risk.trading_hours_utc: must be [start_hour, end_hour]")
    elif not (0 <= hours[0] < hours[1] <= 24):
        errors.append("risk.trading_hours_utc: require 0 <= start < end <= 24")

    kronos = cfg.get("kronos") or {}
    _boolean(errors, kronos, "kronos", "enabled")
    if kronos.get("enabled", False):
        _integer(errors, kronos, "kronos", "ensemble_runs", lo=1, hi=10)
        _number(errors, kronos, "kronos", "min_agreement", lo=0, hi=1)
        _integer(errors, kronos, "kronos", "lookback", lo=60, hi=100000)
        _integer(errors, kronos, "kronos", "pred_len", lo=1, hi=512)
        _integer(errors, kronos, "kronos", "max_context", lo=64, hi=100000, required=False)
        tfs = kronos.get("timeframes")
        if tfs is not None and (not isinstance(tfs, list) or not all(isinstance(x, str) for x in tfs)):
            errors.append("kronos.timeframes: must be a list of timeframe strings")

    news = cfg.get("news") or {}
    _boolean(errors, news, "news", "enabled")
    _number(errors, news, "news", "window_before_min", lo=0, hi=1440, required=False)
    _number(errors, news, "news", "window_after_min", lo=0, hi=1440, required=False)

    telegram = cfg.get("telegram") or {}
    _boolean(errors, telegram, "telegram", "enabled")
    if telegram.get("enabled"):
        if not (telegram.get("bot_token") or telegram.get("token_file")):
            errors.append("telegram: enabled but bot_token and token_file are both empty")

    llm = cfg.get("llm") or {}
    _boolean(errors, llm, "llm", "enabled")
    _string(errors, llm, "llm", "gemini_model", required=False)
    _number(errors, llm, "llm", "timeout_sec", lo=1, hi=120, required=False)

    logging_cfg = _section(cfg, "logging", errors)
    _string(errors, logging_cfg, "logging", "db_file")
    _string(errors, logging_cfg, "logging", "log_file")

    if errors:
        raise ConfigError(errors)
    return cfg
