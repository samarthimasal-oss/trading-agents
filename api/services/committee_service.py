"""Read the runner's committee board (``state/live.json``) without inventing reviews.

This file is written by the legacy runner each poll.  It is not a durable
committee log; the page labels its age and says "no recent review" when the
board is old or absent.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable


class CommitteeService:
    def __init__(self, root: str | Path, config_loader: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.path = self.root / "state" / "live.json"
        self.config_loader = config_loader or (lambda: {})
        self.clock = clock

    def snapshot(self) -> dict:
        if not self.path.exists():
            return {
                "available": False,
                "reason": "state/live.json has not been written by the runner",
                "stale": True,
                "age_seconds": None,
                "symbols": {},
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"available": False, "reason": f"live board unreadable: {exc}",
                    "stale": True, "age_seconds": None, "symbols": {}}
        if not isinstance(data, dict):
            return {"available": False, "reason": "live board has an unknown shape",
                    "stale": True, "age_seconds": None, "symbols": {}}

        now = self.clock()
        ts = float(data.get("ts") or 0.0)
        age = max(0.0, now - ts) if ts else None
        cfg = self.config_loader() or {}
        poll = float((cfg.get("trading") or {}).get("poll_seconds") or 15)
        stale = age is None or age > max(60.0, poll * 4)
        symbols = {}
        for name, row in (data.get("symbols") or {}).items():
            if not isinstance(row, dict):
                continue
            symbols[name] = {
                "price": row.get("price"),
                "atr": row.get("atr"),
                "spread": row.get("spread"),
                "regime": row.get("regime"),
                "direction": row.get("dir"),
                "strength": row.get("strength"),
                "confidence": row.get("conf"),
                "action": row.get("action"),
                "why": row.get("why"),
                "agents": row.get("agents") or {},
                "llm": row.get("llm"),
                "llm_comment": row.get("llm_comment"),
                "kronos_direction": row.get("kronos_dir"),
                "kronos_summary": row.get("kronos_summary"),
                "kronos_agreement": row.get("kronos_agreement"),
                "kronos_runs": row.get("kronos_runs"),
                "news": row.get("news"),
                "htf": row.get("htf") or [],
                "forecast_candles": row.get("forecast_candles") or [],
            }
        return {
            "available": True,
            "stale": stale,
            "age_seconds": age,
            "mode": data.get("mode"),
            "equity": data.get("equity"),
            "daily_pnl": data.get("daily_pnl"),
            "symbols": symbols,
            "warning": ("live board is stale; start the runner for a fresh review"
                        if stale else None),
        }


__all__ = ["CommitteeService"]
