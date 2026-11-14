"""Read and update the allow-listed, non-secret part of config.json."""
from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api.errors import APIError
from api.services import ApiSettings
from core.config_schema import ConfigError, TIMEFRAMES, validate_config

# path -> ("number" | "integer" | "boolean" | "string" | "string_list")
ALLOWED_FIELDS: dict[str, str] = {
    "trading.symbol": "string",
    "trading.symbols": "string_list",
    "trading.timeframe": "timeframe",
    "trading.close_on_opposite": "boolean",
    "trading.require_llm_approval": "boolean",
    "risk.risk_percent_per_trade": "number",
    "risk.fixed_lot": "number",
    "risk.max_positions": "integer",
    "risk.max_open_same_symbol": "integer",
    "risk.max_daily_loss_percent": "number",
    "risk.max_equity_drawdown_percent": "number",
    "risk.max_spread_points": "number",
    "risk.max_notional_leverage_per_position": "number",
    "risk.partial_close_enabled": "boolean",
    "risk.partial_close_at_r": "number",
    "risk.partial_close_pct": "number",
    "risk.time_stop_minutes": "number",
    "risk.breakeven_atr": "number",
    "risk.trailing_atr": "number",
    "risk.trailing_step_atr": "number",
    "llm.enabled": "boolean",
    "llm.gemini_model": "string",
    "kronos.enabled": "boolean",
    "kronos.ensemble_runs": "integer",
    "news.enabled": "boolean",
    "news.window_before_min": "number",
    "news.window_after_min": "number",
    "telegram.enabled": "boolean",
}

_NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "risk.risk_percent_per_trade": (0.01, 5.0),
    "risk.fixed_lot": (0.0, 100.0),
    "risk.max_positions": (1, 20),
    "risk.max_open_same_symbol": (1, 10),
    "risk.max_daily_loss_percent": (0.1, 50.0),
    "risk.max_equity_drawdown_percent": (0.1, 90.0),
    "risk.max_spread_points": (1, 5000),
    "risk.max_notional_leverage_per_position": (0.1, 20.0),
    "risk.partial_close_at_r": (0.1, 20.0),
    "risk.partial_close_pct": (1, 100),
    "risk.time_stop_minutes": (0, 10080),
    "risk.breakeven_atr": (0.1, 20.0),
    "risk.trailing_atr": (0.1, 20.0),
    "risk.trailing_step_atr": (0.01, 10.0),
    "kronos.ensemble_runs": (1, 20),
    "news.window_before_min": (0, 240),
    "news.window_after_min": (0, 240),
}


@dataclass
class SettingsService:
    settings: ApiSettings
    store: Any
    _lock: threading.RLock = threading.RLock()

    # ------------------------------------------------------------------ reads
    def raw(self) -> dict:
        try:
            return json.loads(self.settings.config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise APIError(503, "config_unavailable",
                           "config.json could not be read", str(exc)) from exc
        except ValueError as exc:
            raise APIError(503, "config_invalid",
                           "config.json is not valid JSON", str(exc)) from exc

    def public(self) -> dict:
        cfg = self.raw()
        risk = dict(cfg.get("risk") or {})
        mt5 = dict(cfg.get("mt5") or {})
        llm = dict(cfg.get("llm") or {})
        telegram = dict(cfg.get("telegram") or {})
        trading = dict(cfg.get("trading") or {})
        kronos = dict(cfg.get("kronos") or {})
        news = dict(cfg.get("news") or {})
        ea = dict(cfg.get("ea") or {})
        strategies = dict(cfg.get("strategies") or {})

        token_path = self.settings.root / str(ea.get("token_file") or "state/ea_token.txt")
        settings = {
            "trading": {
                key: trading.get(key) for key in (
                    "symbol", "symbols", "timeframe", "magic",
                    "close_on_opposite", "require_llm_approval",
                )
            },
            "risk": {key: risk.get(key) for key in sorted(risk)},
            "mt5": {
                "login": mt5.get("login"),
                "server": mt5.get("server"),
                "terminal_path": mt5.get("terminal_path"),
                "timeout_ms": mt5.get("timeout_ms"),
                "deviation_points": mt5.get("deviation_points"),
                "password_present": bool(mt5.get("password")),
            },
            "llm": {
                "enabled": llm.get("enabled"),
                "gemini_model": llm.get("gemini_model"),
                "api_key_present": bool(llm.get("api_key") or llm.get("api_key_file")),
                "timeout_seconds": llm.get("timeout_seconds"),
            },
            "kronos": {
                "enabled": kronos.get("enabled"),
                "ensemble_runs": kronos.get("ensemble_runs"),
                "timeframes": kronos.get("timeframes"),
            },
            "news": {
                "enabled": news.get("enabled"),
                "window_before_min": news.get("window_before_min"),
                "window_after_min": news.get("window_after_min"),
                "min_impact": news.get("min_impact"),
            },
            "telegram": {
                "enabled": telegram.get("enabled"),
                "bot_token_present": bool(telegram.get("bot_token")
                                          or telegram.get("token_file")),
                "chat_id": telegram.get("chat_id"),
            },
            "ea": {
                "enabled": ea.get("enabled", False),
                "host": ea.get("host", "127.0.0.1"),
                "port": ea.get("port", 8765),
                "allow_live": ea.get("allow_live", False),
                "token_present": token_path.exists(),
            },
            "strategies": {
                key: strategies.get(key) for key in (
                    "enabled", "min_confidence", "min_rr",
                    "max_signals_per_bar", "allow_opposite_directions",
                )
            },
        }
        return {
            "settings": settings,
            "meta": {
                "config_path": str(self.settings.config_path),
                "restart_required": True,
                "secrets": "redacted",
            },
        }

    # ----------------------------------------------------------------- writes
    def update(self, patch: dict) -> dict:
        if not isinstance(patch, dict) or not patch:
            raise APIError(422, "validation_error",
                           "settings patch must be a non-empty object")
        unknown = sorted(set(patch) - set(ALLOWED_FIELDS))
        if unknown:
            raise APIError(422, "validation_error",
                           "unknown or read-only setting paths",
                           {"unknown": unknown})

        with self._lock:
            cfg = copy.deepcopy(self.raw())
            for path, value in patch.items():
                self._validate_value(path, value)
                self._set_path(cfg, path, value)
            try:
                validate_config(cfg)
            except ConfigError as exc:
                raise APIError(422, "validation_error",
                               "configuration failed validation", exc.errors) from exc
            self._backup()
            self._write_atomic(cfg)

        self.store.audit("local", "settings.update", target="config.json",
                         payload=patch, detail=", ".join(sorted(patch)))
        return self.public()

    @staticmethod
    def _validate_value(path: str, value: Any) -> None:
        kind = ALLOWED_FIELDS[path]
        if kind == "boolean":
            if not isinstance(value, bool):
                raise APIError(422, "validation_error", f"{path} must be true/false")
            return
        if kind == "timeframe":
            if not isinstance(value, str) or value.upper() not in TIMEFRAMES:
                raise APIError(422, "validation_error",
                               f"{path} must be one of {sorted(TIMEFRAMES)}")
            return
        if kind == "string":
            if not isinstance(value, str) or not value.strip():
                raise APIError(422, "validation_error", f"{path} must be a non-empty string")
            return
        if kind == "string_list":
            if (not isinstance(value, list) or not value
                    or any(not isinstance(item, str) or not item.strip() for item in value)):
                raise APIError(422, "validation_error",
                               f"{path} must be a non-empty list of symbols")
            if len(set(item.upper() for item in value)) != len(value):
                raise APIError(422, "validation_error", f"{path} contains duplicates")
            return
        if kind in ("number", "integer"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise APIError(422, "validation_error", f"{path} must be a number")
            if kind == "integer" and float(value) != int(value):
                raise APIError(422, "validation_error", f"{path} must be an integer")
            lo, hi = _NUMERIC_RANGES.get(path, (float("-inf"), float("inf")))
            if not (lo <= float(value) <= hi):
                raise APIError(422, "validation_error",
                               f"{path} must be between {lo} and {hi}")
            return
        raise APIError(422, "validation_error", f"{path} is not editable")

    @staticmethod
    def _set_path(cfg: dict, path: str, value: Any) -> None:
        parts = path.split(".")
        node = cfg
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value.upper() if path == "trading.timeframe" else value

    def _backup(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        target = self.settings.config_path.with_name(
            f"{self.settings.config_path.name}.bak-dashboard-{stamp}")
        shutil.copy2(self.settings.config_path, target)
        return target

    def _write_atomic(self, cfg: dict) -> None:
        path = self.settings.config_path
        tmp = path.with_suffix(path.suffix + ".dashboard-tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        # Preserve the original file's permissions where possible.
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)


__all__ = ["ALLOWED_FIELDS", "SettingsService"]
