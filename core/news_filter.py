"""Economic-calendar guard: block new trades around high-impact releases.

Source: ForexFactory weekly JSON (free, no key):
    https://nfs.faireconomy.media/ff_calendar_thisweek.json
Cached to state/news_calendar.json, refreshed at most every 6h.

Failure-safe by design: on ANY error (network, parse, cache) trading simply
continues — the guard removes itself rather than blocking the system.

Public API:
    news = NewsGuard(cfg)                  # cfg = config["news"]
    blocked, why = news.impacted(symbol)   # False on any failure
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request

log = logging.getLogger("news")

CACHE = os.path.join("state", "news_calendar.json")
URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
REFRESH_AFTER = 6 * 3600.0

# special symbols -> currencies to watch
_CUA = {"XAUUSD": ["USD"], "XAGUSD": ["USD"]}
_LOCK = threading.Lock()


class NewsGuard:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled", True))
        self.before_min = float(self.cfg.get("window_before_min", 10))
        self.after_min = float(self.cfg.get("window_after_min", 10))
        self.min_impact = str(self.cfg.get("min_impact", "High")).title()
        self._events: list[dict] | None = None
        self._loaded_at = 0.0

    # ------------------------------------------------------------------ data
    def _load(self, force: bool = False) -> None:
        """Load/refresh the weekly calendar. Never raises."""
        if not self.enabled:
            return
        with _LOCK:
            now = time.time()
            if not force and self._events and now - self._loaded_at < REFRESH_AFTER:
                return
            # 1) cache on disk, if fresh
            if not force and os.path.exists(CACHE):
                try:
                    with open(CACHE, encoding="utf-8") as fh:
                        blob = json.load(fh)
                    if now - blob["ts"] < REFRESH_AFTER:
                        self._events = blob["events"]
                        self._loaded_at = now
                        return
                except Exception:
                    pass
            # 2) network refresh
            try:
                req = urllib.request.Request(URL, headers={"User-Agent": "TradingAgents/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    rows = json.loads(resp.read().decode("utf-8", "replace"))
                events = [{
                    "title": r.get("title", ""),
                    "currency": (r.get("country") or "").upper(),
                    "impact": (r.get("impact") or "").title(),
                    "ts": _parse_ts(r.get("date") or ""),
                } for r in rows]
                events = [e for e in events if e["ts"] and e["currency"]]
                self._events = events
                self._loaded_at = now
                os.makedirs("state", exist_ok=True)
                with open(CACHE, "w", encoding="utf-8") as fh:
                    json.dump({"ts": now, "events": events}, fh)
                high = sum(1 for e in events if e["impact"].lower() == "high")
                log.info("news calendar loaded: %d events (%d high-impact)", len(events), high)
            except Exception as exc:                       # noqa: BLE001
                # keep trading — fall back to whatever cache exists
                if self._events is None:
                    try:
                        with open(CACHE, encoding="utf-8") as fh:
                            self._events = json.load(fh)["events"]
                    except Exception:
                        self._events = []
                log.warning("news calendar unavailable (%s) — trading continues", exc)

    # ------------------------------------------------------------------ gate
    def impacted(self, symbol: str) -> tuple[bool, str]:
        """True if a high-impact release for this symbol's currencies is within
        the configured window (before +- after). Never raises."""
        if not self.enabled:
            return False, ""
        try:
            self._load()
            if not self._events:
                return False, ""
            sym = symbol.upper()
            if sym in _CUA:
                currencies = set(_CUA[sym])
            else:
                currencies = {sym[:3], sym[3:6]}           # e.g. EURUSD -> {EUR, USD}
            now = time.time()
            for e in self._events:
                if e["impact"].lower() != self.min_impact.lower():
                    continue
                if e["currency"] not in currencies:
                    continue
                dt = now - e["ts"]
                if -self.before_min * 60 <= dt <= self.after_min * 60:
                    why = (f"news window: {e['currency']} {e['title']} "
                           f"({'upcoming' if dt < 0 else 'just released'})")
                    return True, why
        except Exception as exc:                           # noqa: BLE001
            log.warning("news gate error (trading continues): %s", exc)
        return False, ""


def _parse_ts(raw: str) -> float:
    """ForexFactory date strings look like '2026-10-27T12:30:00-04:00' or
    ISO with Z/offset."""
    try:
        from datetime import datetime
        s = raw.replace("T", " ")
        if s.endswith("Z"):
            s = s[:-1]
        # strip manual "-04:00"/"+02:00" if present, then parse as local->UTC naive
        def _tz(txt: str):
            seg = txt[-6:]
            if (seg[:1] in "+-") and (":" in seg):
                hh, mm = seg[1:].split(":")
                off = (int(hh) * 60 + (int(mm) if seg[0] == "+" else -int(mm))) * 60
                return off, txt[:-6]
            return 0, txt
        off, core = _tz(raw if "T" in raw else s)
        dt = datetime.fromisoformat(core.replace("Z", "").replace("T", " "))
        import calendar
        return calendar.timegm(dt.timetuple()) - off
    except Exception:
        return 0.0
