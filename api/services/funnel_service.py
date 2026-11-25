"""Real-data funnel: signals → agents → Kronos → Gemini → risk → execution → closed.

Every count comes from something the system actually recorded:

* ``evaluations`` rows are written by the runner once per stage per closed bar
  (including the rejection reason and the direction/confidence at that point);
* order counts come from the journal's ``events`` table;
* closed trades come from the terminal's deal history, never from modelling.

If the runner has not written any stage rows yet the API says so explicitly
instead of inventing a funnel.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable

STAGES: tuple[tuple[str, str], ...] = (
    ("signal", "Signals generated"),
    ("agents", "Agent votes"),
    ("kronos", "Kronos forecast"),
    ("gemini", "Gemini review"),
    ("risk", "Risk gate"),
    ("execution", "Execution"),
    ("closed", "Closed trades"),
)

_PASS = {"pass", "filled", "advisory", "approved", "win"}
_REJECT = {"reject", "veto", "loss"}


class FunnelService:
    def __init__(self, journal: Any, mt5: Any = None,
                 clock: Callable[[], float] = time.time,
                 cache_seconds: float = 20.0, magic: int | None = None):
        self.journal = journal
        self.mt5 = mt5
        self.clock = clock
        self.cache_seconds = max(0.0, float(cache_seconds))
        self.magic = int(magic) if magic else None
        self._lock = threading.Lock()
        self._cache: tuple[float, tuple, dict] | None = None

    # ------------------------------------------------------------------ helpers
    def _closed_rows(self, hours: int, symbol: str | None) -> tuple[list[dict] | None, str | None]:
        if self.mt5 is None:
            return None, "the MT5 adapter is not wired into the dashboard"
        days = max(1, int(math.ceil(hours / 24.0)))
        try:
            if self.magic:
                rows = list(self.mt5.closed_positions(days=days, magic=self.magic))
            else:
                rows = list(self.mt5.closed_positions(days=days))
        except Exception as exc:                        # noqa: BLE001
            return None, f"MT5 deal history unavailable: {exc}"
        since = self.clock() - hours * 3600
        wanted = (symbol or "").upper()
        out = []
        for row in rows:
            try:
                close_time = float(row.get("close_time") or 0.0)
            except (TypeError, ValueError):
                continue
            if close_time and close_time < since:
                continue
            row_symbol = str(row.get("symbol") or "").upper()
            if wanted and not row_symbol.startswith(wanted):
                continue
            out.append(row)
        return out, None

    @staticmethod
    def _stage(key: str, label: str, outcomes: dict[str, int]) -> dict:
        reached = sum(outcomes.values())
        passed = sum(n for outcome, n in outcomes.items() if outcome in _PASS)
        rejected = sum(n for outcome, n in outcomes.items() if outcome in _REJECT)
        skipped = sum(n for outcome, n in outcomes.items() if outcome == "skip")
        return {
            "key": key,
            "label": label,
            "reached": reached,
            "passed": passed,
            "rejected": rejected,
            "skipped": skipped,
            "outcomes": outcomes,
        }

    def _by_symbol(self, rows: list[dict]) -> list[dict]:
        table: dict[str, dict] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "?")
            entry = table.setdefault(symbol, {"symbol": symbol, "signals": 0,
                                              "executed": 0, "rejected": 0,
                                              "closed": 0})
            stage, outcome, count = row["stage"], row["outcome"], int(row["count"])
            if stage == "signal" and outcome in _PASS:
                entry["signals"] += count
            elif stage == "execution" and outcome == "filled":
                entry["executed"] += count
            elif outcome in _REJECT:
                entry["rejected"] += count
        return sorted(table.values(), key=lambda item: (-item["signals"],
                                                        item["symbol"]))

    def _snapshot_uncached(self, hours: int, symbol: str | None) -> dict:
        now = self.clock()
        since = now - hours * 3600
        try:
            raw = self.journal.funnel(since=since, symbol=symbol)
        except Exception as exc:                        # noqa: BLE001
            raw = {"available": False, "reason": str(exc), "rows": [],
                   "reasons": [], "by_symbol": [], "latest_ts": None,
                   "evaluations": 0}
        try:
            events = self.journal.event_counts(since=since)
        except Exception:                               # noqa: BLE001
            events = {}

        outcomes: dict[str, dict[str, int]] = {}
        for row in raw.get("rows") or []:
            stage = str(row.get("stage") or "")
            outcomes.setdefault(stage, {})[str(row.get("outcome") or "")] = \
                int(row.get("count") or 0)

        stages = [self._stage(key, label, outcomes.get(key, {}))
                  for key, label in STAGES]

        closed_rows, closed_error = self._closed_rows(hours, symbol)
        closed_stage = stages[-1]
        if closed_rows is None:
            closed_stage.update({"available": False, "reason": closed_error,
                                 "reached": 0, "passed": 0, "rejected": 0})
        else:
            wins = sum(1 for row in closed_rows if float(row.get("net") or 0.0) > 0)
            losses = len(closed_rows) - wins
            net = sum(float(row.get("net") or 0.0) for row in closed_rows)
            closed_stage.update({
                "available": True,
                "reached": len(closed_rows),
                "passed": wins,
                "rejected": losses,
                "win_rate": (wins / len(closed_rows) * 100.0) if closed_rows else None,
                "net": net,
            })

        rejections = [
            {"stage": row["stage"], "reason": str(row["reason"])[:240],
             "count": int(row["count"])}
            for row in (raw.get("reasons") or [])
        ]
        by_symbol = self._by_symbol(raw.get("by_symbol") or [])
        if closed_rows:
            index = {entry["symbol"]: entry for entry in by_symbol}
            for row in closed_rows:
                name = str(row.get("symbol") or "?")
                entry = index.setdefault(name, {"symbol": name, "signals": 0,
                                                "executed": 0, "rejected": 0,
                                                "closed": 0})
                entry["closed"] += 1

        available = bool(raw.get("available", getattr(self.journal, "available", False)))
        evaluations = int(raw.get("evaluations") or 0)
        if not available:
            note = "the runner journal does not exist yet"
        elif evaluations == 0:
            note = ("no funnel rows recorded yet: the upgraded runner writes one "
                    "stage row per symbol per closed bar")
        else:
            note = ("stage counts are written by the runner; closed trades come "
                    "from MT5 deal history")

        return {
            "available": available,
            "generated_at": now,
            "window_hours": hours,
            "since": since,
            "symbol": symbol.upper() if symbol else None,
            "latest_ts": raw.get("latest_ts"),
            "evaluations": evaluations,
            "stages": stages,
            "rejections": rejections,
            "by_symbol": by_symbol,
            "orders": {
                "filled": int(events.get("ORDER_FILLED", 0)),
                "rejected": int(events.get("ORDER_REJECTED", 0)),
                "closed": int(events.get("ORDER_CLOSED", 0)),
                "partial_closes": int(events.get("PARTIAL_CLOSE", 0)),
                "sl_modified": int(events.get("SL_MODIFIED", 0)),
            },
            "note": note,
        }

    # ------------------------------------------------------------------- public
    def snapshot(self, hours: int = 24, symbol: str | None = None) -> dict:
        hours = max(1, min(int(hours), 24 * 30))
        symbol = (symbol or "").strip().upper() or None
        key = (hours, symbol)
        now = self.clock()
        with self._lock:
            if (self._cache and now - self._cache[0] <= self.cache_seconds
                    and self._cache[1] == key):
                cached = dict(self._cache[2])
                cached["cached"] = True
                return cached
        payload = self._snapshot_uncached(hours, symbol)
        payload["cached"] = False
        with self._lock:
            self._cache = (now, key, dict(payload))
        return payload


__all__ = ["FunnelService", "STAGES"]
