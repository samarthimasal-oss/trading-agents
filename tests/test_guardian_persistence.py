"""Guardian lockout must survive a process restart."""
from __future__ import annotations

import os
import tempfile

from core.models import AccountSnapshot
from core.state_store import StateStore
from execution.guardian import Guardian


RISK = {
    "max_daily_loss_percent": 3.0,
    "max_equity_drawdown_percent": 15.0,
}


class FakeClient:
    def __init__(self, equity: float, daily: float):
        self.equity = equity
        self.daily = daily

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(login=1, balance=self.equity, equity=self.equity)

    def daily_bot_profit(self, magic: int) -> float:
        return self.daily


def test_lockout_survives_restart(tmp_path):
    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        first = Guardian({"risk": RISK}, FakeClient(10_000.0, -400.0), store)
        first.magic = 1
        allowed, reason, flatten = first.check()
        assert not allowed
        assert flatten
        assert first.is_halted()[0]

        # simulate a full process restart: new Guardian, same journal
        second = Guardian({"risk": RISK}, FakeClient(10_000.0, 0.0), store)
        second.magic = 1
        halted, why = second.is_halted()
        assert halted, "daily lockout was lost on restart"
        assert "daily limit" in why or "lockout" in why
        allowed2, _, flatten2 = second.check()
        assert not allowed2
        assert flatten2

        # manual reset must also persist
        second.reset()
        third = Guardian({"risk": RISK}, FakeClient(10_000.0, 0.0), store)
        third.magic = 1
        assert not third.is_halted()[0]
    finally:
        store.close()


def test_day_rollover_clears_old_lockout(tmp_path):
    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        g = Guardian({"risk": RISK}, FakeClient(10_000.0, 0.0), store)
        g.magic = 1
        g._halted_day = "2000-01-01"
        g._halt_reason = "old"
        g._last_day = "2000-01-01"
        allowed, _, _ = g.check()
        assert allowed
        assert not g.is_halted()[0]
        state = store.guardian_state()
        assert state["halted_day"] is None
        assert state["day_start_equity"] == 10_000.0
    finally:
        store.close()
