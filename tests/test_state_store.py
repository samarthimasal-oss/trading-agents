"""StateStore guardian persistence round-trip."""
from __future__ import annotations

from core.state_store import StateStore


def test_guardian_state_roundtrip(tmp_path):
    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        assert store.guardian_state() is None
        store.save_guardian_state(day="2026-11-02", day_start_equity=10000.0,
                                  halted_day="2026-11-02", halt_reason="test",
                                  halted_at=123.0)
        state = store.guardian_state()
        assert state["day"] == "2026-11-02"
        assert state["day_start_equity"] == 10000.0
        assert state["halted_day"] == "2026-11-02"
        assert state["halt_reason"] == "test"

        store.save_guardian_state(day="2026-11-02", day_start_equity=10000.0,
                                  halted_day=None, halt_reason="")
        assert store.guardian_state()["halted_day"] is None
    finally:
        store.close()
