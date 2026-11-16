"""Learning Centre data, derived from the MT5 deal history.

The dashboard has no closed-trade store of its own yet, so the page builds its
rows from what the terminal can prove: the account's deal history
(``history_deals_get``) plus the entry order's SL/TP.  Fields the deal record
cannot provide (strategy, MFE/MAE, exit reason, committee reasoning) are
reported as explicitly missing instead of being inferred.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WINDOW_DAYS = 180

# Fields the dashboard cannot produce from deal history today.  Phase 6 must
# record closed trades in the runner journal before these exist.
_MISSING = [
    "strategy",
    "mfe",
    "mae",
    "exit_reason",
    "spread_at_entry",
    "slippage",
    "committee_reasoning",
    "loss_classification",
]


def _session_of(ts: float) -> str:
    """UTC session label; boundaries match the market overview page."""
    hour = datetime.fromtimestamp(float(ts), tz=timezone.utc).hour
    if 0 <= hour < 7:
        return "tokyo"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 16:
        return "london/new-york overlap"
    if 16 <= hour < 21:
        return "new-york"
    return "late-session"


def _risk_amount(row: dict, settings: dict | None) -> float | None:
    """Risk of the trade in account currency, from the entry order's SL."""
    sl = row.get("sl")
    volume = float(row.get("volume") or 0.0)
    entry = float(row.get("entry_price") or 0.0)
    if sl is None or float(sl) <= 0.0 or volume <= 0.0 or not settings:
        return None
    tick_size = float(settings.get("trade_tick_size") or 0.0)
    tick_value = float(settings.get("trade_tick_value") or 0.0)
    if tick_size <= 0.0 or tick_value <= 0.0:
        return None
    distance = abs(entry - float(sl))
    if distance <= 0.0:
        return None
    ticks = distance / tick_size
    return ticks * tick_value * volume


def _realized_r(row: dict, settings: dict | None) -> tuple[float | None, str | None]:
    risk = _risk_amount(row, settings)
    if risk is None:
        return None, None
    net = float(row.get("net") or 0.0)
    return net / risk, "entry-order SL"


class LearningService:
    def __init__(self, root: str | Path, journal: Any = None, mt5: Any = None,
                 magic: int = 0, clock: Any = time.time):
        self.root = Path(root)
        self.journal = journal
        self.mt5 = mt5
        self.magic = int(magic or 0)
        self.clock = clock

    # ------------------------------------------------------------------ plumbing
    def _closed(self) -> tuple[list[dict] | None, str | None]:
        if self.mt5 is None:
            return None, "the MT5 adapter is not wired into the dashboard"
        try:
            return list(self.mt5.closed_positions(days=WINDOW_DAYS)), None
        except Exception as exc:                            # noqa: BLE001
            return None, f"MT5 deal history unavailable: {exc}"

    def _settings_cache(self, symbols: list[str]) -> dict[str, dict | None]:
        cache: dict[str, dict | None] = {}
        for symbol in dict.fromkeys(symbols):
            try:
                cache[symbol] = self.mt5.symbol_settings(symbol)
            except Exception:                               # noqa: BLE001
                cache[symbol] = None
        return cache

    def _rows(self, limit: int) -> tuple[list[dict], dict[str, Any], str | None]:
        closed, reason = self._closed()
        if closed is None:
            return [], {"available": False, "reason": reason}, reason
        rows: list[dict] = []
        cache = self._settings_cache([str(row.get("symbol") or "") for row in closed])
        for row in closed[: max(1, min(int(limit), 500))]:
            settings = cache.get(str(row.get("symbol") or ""))
            realized_r, basis = _realized_r(row, settings)
            rows.append({
                **row,
                "session": _session_of(float(row.get("open_time") or 0.0)),
                "is_bot": self.magic > 0 and int(row.get("magic") or 0) == self.magic,
                "realized_r": realized_r,
                "risk_basis": basis,
                "digits": (settings or {}).get("digits"),
            })
        meta: dict[str, Any] = {
            "available": True,
            "data_source": f"MT5 deal history on this account (last {WINDOW_DAYS} days)",
            "window_days": WINDOW_DAYS,
        }
        if not rows:
            meta["reason"] = (f"no closed trades on this account in the last "
                              f"{WINDOW_DAYS} days")
        return rows, meta, None

    @staticmethod
    def _metrics(rows: list[dict]) -> dict:
        wins = [row for row in rows if float(row.get("net") or 0.0) > 0.0]
        losses = [row for row in rows if float(row.get("net") or 0.0) <= 0.0]
        r_values = [float(row["realized_r"]) for row in rows
                    if row.get("realized_r") is not None
                    and float(row["realized_r"]) == float(row["realized_r"])]
        sessions: dict[str, int] = {}
        symbols: dict[str, int] = {}
        for row in rows:
            session = str(row.get("session") or "unknown")
            sessions[session] = sessions.get(session, 0) + 1
            symbol = str(row.get("symbol") or "unknown")
            symbols[symbol] = symbols.get(symbol, 0) + 1
        bot = [row for row in rows if row.get("is_bot")]
        return {
            "closed_trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(rows) * 100.0) if rows else None,
            "total_net": sum(float(row.get("net") or 0.0) for row in rows),
            "avg_r": (sum(r_values) / len(r_values)) if r_values else None,
            "r_known": len(r_values),
            "sessions": sessions,
            "symbols": symbols,
            "bot_trades": len(bot),
            "non_bot_trades": len(rows) - len(bot),
        }

    # ------------------------------------------------------------------- public
    def summary(self) -> dict:
        rows, meta, reason = self._rows(limit=500)
        if not meta.get("available", False):
            return {
                "available": False,
                "reason": reason or meta.get("reason") or "closed-trade data unavailable",
                "missing_fields": list(_MISSING),
                "closed_trades": None,
                "metrics": None,
                "next_step": ("implement the closed-trade table in the runner "
                              "journal and backfill from MT5 deal history"),
            }
        metrics = self._metrics(rows)
        payload = {
            **meta,
            "closed_trades": metrics["closed_trades"],
            "metrics": metrics,
            "missing_fields": list(_MISSING),
            "attribution_note": ("rows come from the terminal's deal history; "
                                 "strategy and committee attribution needs the "
                                 "Phase 6 closed-trade store"),
        }
        if not rows:
            payload["note"] = (f"the page will list closed trades as soon as the "
                               f"account books one; nothing is synthesised in "
                               f"the meantime")
        journal = getattr(self.journal, "available", None)
        if journal is not None:
            payload["journal_available"] = bool(journal)
        return payload

    def trades(self, limit: int = 100) -> dict:
        rows, meta, reason = self._rows(limit=limit)
        if not meta.get("available", False):
            return {
                "available": False,
                "reason": reason or meta.get("reason") or "closed-trade data unavailable",
                "missing_fields": list(_MISSING),
                "trades": [],
                "limit": max(1, min(int(limit), 1000)),
            }
        return {
            **meta,
            "missing_fields": list(_MISSING),
            "trades": rows,
            "count": len(rows),
            "limit": max(1, min(int(limit), 1000)),
        }


__all__ = ["LearningService"]
