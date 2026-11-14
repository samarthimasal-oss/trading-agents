"""Risk snapshot and the guarded emergency close-all path."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from api.errors import APIError
from api.services import ApiSettings
from api.services.settings_service import SettingsService

_PEAK_TTL_SECONDS = 60.0
_DAILY_TTL_SECONDS = 15.0


class RiskService:
    def __init__(self, settings: ApiSettings, store: Any, mt5: Any, journal: Any,
                 runner: Any, config: SettingsService,
                 state_store: Any = None):
        self.settings = settings
        self.store = store
        self.mt5 = mt5
        self.journal = journal
        self.runner = runner
        self.config = config
        self.state_store = state_store
        self._cache_lock = threading.Lock()
        self._peak_cache: tuple[float, float | None, float | None] = (0.0, None, None)
        self._daily_cache: tuple[float, float | None, str | None] = (0.0, None, None)

    def snapshot(self) -> dict:
        cfg = self.config.raw()
        risk = dict(cfg.get("risk") or {})
        trading = dict(cfg.get("trading") or {})
        magic = trading.get("magic")
        account = self.mt5.account()
        positions = self.mt5.positions(magic=magic)
        guardian = self.journal.guardian()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        daily_pnl, daily_pnl_source = self._daily_pnl(account, guardian, day)
        peak_equity, peak_equity_ts = self._peak_equity(account)
        equity = account.get("equity")
        drawdown_pct = None
        if peak_equity and peak_equity > 0 and equity is not None:
            drawdown_pct = max(0.0, (float(peak_equity) - float(equity))
                               / float(peak_equity) * 100.0)

        return {
            "limits": risk,
            "guardian": guardian,
            "account": account,
            "open_positions": len(positions),
            "equity": equity,
            "day": day,
            "daily_loss_limit_usd": (
                float(risk.get("max_daily_loss_percent") or 0.0) / 100.0
                * float(equity or 0.0)
            ),
            "daily_pnl": daily_pnl,
            "daily_pnl_source": daily_pnl_source,
            "peak_equity": peak_equity,
            "peak_equity_ts": peak_equity_ts,
            "drawdown_from_peak_pct": drawdown_pct,
            "magic": magic,
            "runner": self.runner.status(),
        }

    def _daily_pnl(self, account: dict, guardian: dict,
                   day: str) -> tuple[float | None, str | None]:
        """Prefer the guardian's day-start equity; fall back to MT5 deals.

        The fallback sums closed deals for the bot magic today plus the
        floating P/L, which is exactly the number the daily lockout watches.
        """
        start = guardian.get("day_start_equity") if isinstance(guardian, dict) else None
        if (isinstance(guardian, dict) and guardian.get("available")
                and guardian.get("day") == day and start is not None):
            try:
                return float(account.get("equity") or 0.0) - float(start), "guardian day start"
            except (TypeError, ValueError):
                pass
        now = time.time()
        with self._cache_lock:
            cached_at, cached_value, cached_source = self._daily_cache
            if now - cached_at <= _DAILY_TTL_SECONDS:
                return cached_value, cached_source
        try:
            value = float(self.mt5.daily_bot_profit())
            source = "MT5 deals today + floating"
        except Exception:                                    # noqa: BLE001
            value, source = None, None
        with self._cache_lock:
            self._daily_cache = (now, value, source)
        return value, source

    def _peak_equity(self, account: dict) -> tuple[float | None, float | None]:
        now = time.time()
        with self._cache_lock:
            cached_at, peak, peak_ts = self._peak_cache
            fresh = now - cached_at <= _PEAK_TTL_SECONDS
        if not fresh:
            peak = None
            peak_ts = None
            for row in self.journal.equity(limit=5000):
                try:
                    value = float(row.get("equity"))
                except (TypeError, ValueError):
                    continue
                if peak is None or value > peak:
                    peak = value
                    peak_ts = row.get("ts")
        try:
            current = float(account.get("equity"))
        except (TypeError, ValueError):
            current = None
        if current is not None and (peak is None or current > peak):
            peak = current
            peak_ts = None
        with self._cache_lock:
            self._peak_cache = (now, peak, peak_ts)
        return peak, peak_ts

    def emergency_close(self, *, dry_run: bool = True,
                        confirm: str = "") -> dict:
        cfg = self.config.raw()
        magic = (cfg.get("trading") or {}).get("magic")
        positions = self.mt5.positions(magic=magic)
        symbols = sorted({str(getattr(p, "get", lambda *_: "")("symbol", ""))
                          if isinstance(p, dict) else str(getattr(p, "symbol", ""))
                          for p in positions})
        symbols = [symbol for symbol in symbols if symbol]
        if dry_run:
            result = {
                "dry_run": True,
                "positions": len(positions),
                "closed": 0,
                "symbols": symbols,
                "message": "DRY-RUN: no position was closed",
            }
            self.store.audit("local", "risk.emergency_close", target="dry-run",
                             payload={"dry_run": True}, detail="dry-run")
            return result
        if str(confirm or "").strip() != "CLOSE ALL":
            raise APIError(400, "confirmation_required",
                           'a real close requires confirm="CLOSE ALL"')
        closed = int(self.mt5.close_all(magic=magic))
        result = {
            "dry_run": False,
            "positions": len(positions),
            "closed": closed,
            "symbols": symbols,
            "message": f"closed {closed} position(s)",
        }
        self.store.audit("local", "risk.emergency_close", target="demo",
                         payload={"dry_run": False}, detail=f"closed={closed}")
        return result

    def reset_guardian(self) -> dict:
        if self.state_store is None:
            raise APIError(503, "guardian_store_unavailable",
                           "guardian state store is not configured")
        self.state_store.save_guardian_state(
            day=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).strftime("%Y-%m-%d"),
            day_start_equity=None, halted_day=None, halt_reason="",
            halted_at=None,
        )
        self.store.audit("local", "risk.reset_guardian", target="guardian",
                         detail="halt cleared")
        return {"ok": True, "guardian": self.journal.guardian()}


__all__ = ["RiskService"]
