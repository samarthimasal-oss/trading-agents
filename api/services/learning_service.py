"""Learning Centre data.

There is no closed-trade store yet.  The service therefore reports exactly
which fields cannot be produced (strategy attribution, R, MFE/MAE, exit
reason) instead of deriving plausible-looking numbers from open positions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_MISSING = [
    "strategy",
    "realized_r",
    "mfe",
    "mae",
    "exit_reason",
    "committee_reasoning",
    "loss_classification",
]


class LearningService:
    def __init__(self, root: str | Path, journal: Any = None):
        self.root = Path(root)
        self.journal = journal

    def summary(self) -> dict:
        return {
            "available": False,
            "reason": ("no closed-trade learning store exists yet; "
                       "Phase 6 must record closed trades before this page "
                       "can report R, MFE/MAE or loss classifications"),
            "missing_fields": list(_MISSING),
            "closed_trades": None,
            "metrics": None,
            "next_step": "implement the closed-trade table and backfill from MT5 deal history",
        }

    def trades(self, limit: int = 100) -> dict:
        return {
            "available": False,
            "reason": "no closed-trade learning store exists yet",
            "missing_fields": list(_MISSING),
            "trades": [],
            "limit": max(1, min(int(limit), 1000)),
        }


__all__ = ["LearningService"]
