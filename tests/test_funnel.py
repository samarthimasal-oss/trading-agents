"""Funnel analytics: journal storage, aggregation and the API surface.

The funnel is only allowed to report what the system actually recorded.  These
tests pin that contract: stage rows come from ``evaluations``, order counts from
``events``, and closed trades from MT5 deal history — an empty journal yields an
explicit "no rows yet" note instead of a fabricated conversion rate.
"""
from __future__ import annotations

from types import SimpleNamespace

import time

from api.routers.research import funnel as funnel_endpoint
from api.services.funnel_service import FunnelService
from api.services.journal_adapter import JournalAdapter
from core.state_store import StateStore


NOW = 1_000_000.0


def _row(symbol="XAUUSD", stage="signal", outcome="pass", reason="",
         direction=1, confidence=0.5, bar_time=0.0, detail=""):
    return dict(symbol=symbol, stage=stage, outcome=outcome, reason=reason,
                direction=direction, confidence=confidence, bar_time=bar_time,
                detail=detail)


def _seed(store: StateStore) -> None:
    rows = [
        _row(stage="signal", outcome="pass", detail="strength=0.61"),
        _row(stage="signal", outcome="pass"),
        _row(symbol="EURUSD", stage="signal", outcome="reject",
             reason="no consensus (strength +0.10)"),
        _row(stage="agents", outcome="pass", detail="trend:1,momentum:1"),
        _row(stage="kronos", outcome="pass", direction=1, confidence=0.7),
        _row(stage="kronos", outcome="skip", reason="no forecast"),
        _row(stage="gemini", outcome="advisory", reason="advisory veto ignored"),
        _row(stage="risk", outcome="pass", detail="volume=0.16 sl=2000.0"),
        _row(symbol="EURUSD", stage="risk", outcome="reject",
             reason="cooldown active"),
        _row(stage="execution", outcome="filled", detail="BUY 0.16 XAUUSD"),
    ]
    # one row per stage per symbol per bar: give every row its own closed bar
    for index, row in enumerate(rows):
        row["bar_time"] = NOW - 900 * (len(rows) - index)
        store.evaluation(**row)
    store.event(kind="ORDER_FILLED", symbol="XAUUSD", detail="BUY 0.16",
                retcode=10009, price=2000.2, volume=0.16)
    store.event(kind="ORDER_REJECTED", symbol="EURUSD", detail="cooldown",
                retcode=10018)
    store.event(kind="ORDER_CLOSED", symbol="XAUUSD", detail="close #1")


# ------------------------------------------------------------------ state store
def test_evaluation_rows_roundtrip_with_filters(tmp_path):
    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        _seed(store)
        rows = store.evaluations(limit=50)
        assert len(rows) == 10
        assert rows[0]["stage"] == "execution"
        xau = [r for r in store.evaluations(limit=50, symbol="XAUUSD")]
        assert len(xau) == 8
        assert {r["stage"] for r in xau} >= {"signal", "agents", "kronos",
                                             "gemini", "risk", "execution"}
        # newest first, capped
        assert len(store.evaluations(limit=3)) == 3
    finally:
        store.close()


def test_evaluation_is_idempotent_per_bar_and_stage(tmp_path):
    """A restart inside the same bar must replace, not double-count, a stage."""
    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        store.evaluation(symbol="XAUUSD", stage="risk", outcome="reject",
                         reason="cooldown", bar_time=100.0)
        store.evaluation(symbol="XAUUSD", stage="risk", outcome="pass",
                         bar_time=100.0)
        rows = store.evaluations()
        assert len(rows) == 1
        assert rows[0]["outcome"] == "pass"
        assert rows[0]["reason"] == ""
        # a different bar or stage is a separate row
        store.evaluation(symbol="XAUUSD", stage="risk", outcome="pass",
                         bar_time=200.0)
        store.evaluation(symbol="XAUUSD", stage="signal", outcome="pass",
                         bar_time=100.0)
        assert len(store.evaluations()) == 3
    finally:
        store.close()


# --------------------------------------------------------------- journal adapter
def test_journal_adapter_aggregates_stages_reasons_and_events(tmp_path):
    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        _seed(store)
    finally:
        store.close()
    journal = JournalAdapter(tmp_path / "journal.sqlite")

    data = journal.funnel(since=NOW - 86400)
    assert data["available"] is True
    assert data["evaluations"] == 10
    counts = {(row["stage"], row["outcome"]): row["count"]
              for row in data["rows"]}
    assert counts[("signal", "pass")] == 2
    assert counts[("signal", "reject")] == 1
    assert counts[("kronos", "skip")] == 1
    assert counts[("execution", "filled")] == 1

    reasons = {(row["stage"], row["reason"]): row["count"]
               for row in data["reasons"]}
    assert reasons[("signal", "no consensus (strength +0.10)")] == 1
    assert reasons[("risk", "cooldown active")] == 1
    # advisory/pass outcomes must never appear as rejection reasons
    assert all(row["reason"] for row in data["reasons"])

    assert journal.event_counts(since=time.time() + 60) == {}
    events = journal.event_counts()
    assert events["ORDER_FILLED"] == 1
    assert events["ORDER_REJECTED"] == 1
    assert events["ORDER_CLOSED"] == 1

    xau = journal.funnel(since=NOW - 86400, symbol="xauusd")
    assert xau["evaluations"] == 8
    assert journal.evaluations(limit=2, symbol="EURUSD")[0]["stage"] == "risk"


def test_journal_adapter_missing_db_is_empty_not_an_error(tmp_path):
    journal = JournalAdapter(tmp_path / "missing.sqlite")
    assert journal.available is False
    data = journal.funnel()
    assert data["available"] is False
    assert data["rows"] == []
    assert journal.event_counts() == {}
    assert journal.evaluations() == []


# ----------------------------------------------------------------- funnel service
class FakeMT5:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.calls: list[dict] = []

    def closed_positions(self, days: int = 180, magic: int | None = None):
        self.calls.append({"days": days, "magic": magic})
        if self.error:
            raise self.error
        return list(self.rows)


def _closed(symbol="XAUUSD", net=10.0, close_time=NOW - 3600):
    return {"symbol": symbol, "net": net, "close_time": close_time}


def test_funnel_service_builds_real_stages_and_closed_trades(tmp_path):
    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        _seed(store)
    finally:
        store.close()
    mt5 = FakeMT5([
        _closed(net=10.0),
        _closed(net=-5.0, close_time=NOW - 7200),
        _closed(net=99.0, close_time=NOW - 3 * 86400),        # outside window
        _closed(symbol="EURUSD", net=7.0),                    # filtered out
    ])
    service = FunnelService(JournalAdapter(tmp_path / "journal.sqlite"), mt5,
                            clock=lambda: NOW, cache_seconds=0, magic=260101)
    data = service.snapshot(hours=24, symbol="XAUUSD")
    assert mt5.calls == [{"days": 1, "magic": 260101}]   # bot deals only

    stages = {stage["key"]: stage for stage in data["stages"]}
    assert [stage["key"] for stage in data["stages"]] == [
        "signal", "agents", "kronos", "gemini", "risk", "execution", "closed"]
    assert stages["signal"]["reached"] == 2
    assert stages["signal"]["passed"] == 2
    assert stages["agents"]["passed"] == 1
    assert stages["kronos"]["passed"] == 1
    assert stages["kronos"]["skipped"] == 1
    assert stages["gemini"]["passed"] == 1          # advisory still passes
    assert stages["risk"]["passed"] == 1
    assert stages["execution"]["passed"] == 1
    assert stages["closed"]["available"] is True
    assert stages["closed"]["reached"] == 2
    assert stages["closed"]["passed"] == 1          # winning trade
    assert stages["closed"]["rejected"] == 1        # losing trade
    assert stages["closed"]["win_rate"] == 50.0
    assert stages["closed"]["net"] == 5.0

    assert data["orders"] == {"filled": 1, "rejected": 1, "closed": 1,
                              "partial_closes": 0, "sl_modified": 0}
    assert data["symbol"] == "XAUUSD"
    assert data["evaluations"] == 8
    by_symbol = {entry["symbol"]: entry for entry in data["by_symbol"]}
    assert by_symbol["XAUUSD"]["signals"] == 2
    assert by_symbol["XAUUSD"]["executed"] == 1
    assert by_symbol["XAUUSD"]["closed"] == 2


def test_funnel_service_counts_rejections_and_advisory_vetoes(tmp_path):
    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        store.evaluation(symbol="XAUUSD", stage="signal", outcome="pass")
        store.evaluation(symbol="XAUUSD", stage="risk", outcome="reject",
                         reason="daily loss lockout")
        store.evaluation(symbol="XAUUSD", stage="gemini", outcome="veto",
                         reason="news risk")
    finally:
        store.close()
    service = FunnelService(JournalAdapter(tmp_path / "journal.sqlite"), None,
                            clock=lambda: NOW)
    data = service.snapshot(hours=12, symbol="XAUUSD")
    stages = {stage["key"]: stage for stage in data["stages"]}
    assert stages["risk"]["rejected"] == 1
    assert stages["gemini"]["rejected"] == 1
    reasons = {(row["stage"], row["reason"]) for row in data["rejections"]}
    assert ("risk", "daily loss lockout") in reasons
    assert ("gemini", "news risk") in reasons
    # the terminal is not wired in this unit test; do not pretend otherwise
    assert stages["closed"]["available"] is False
    assert "MT5" in stages["closed"]["reason"]


def test_funnel_service_explains_when_no_rows_exist(tmp_path):
    store = StateStore(str(tmp_path / "journal.sqlite"))
    store.close()                                       # schema only, no rows
    service = FunnelService(JournalAdapter(tmp_path / "journal.sqlite"), None,
                            clock=lambda: NOW)
    data = service.snapshot(hours=24)
    assert data["available"] is True
    assert data["evaluations"] == 0
    assert "no funnel rows recorded yet" in data["note"]
    assert all(stage["reached"] == 0 for stage in data["stages"])


def test_funnel_service_missing_journal_is_explicit(tmp_path):
    service = FunnelService(JournalAdapter(tmp_path / "missing.sqlite"), None,
                            clock=lambda: NOW)
    data = service.snapshot(hours=24)
    assert data["available"] is False
    assert "does not exist yet" in data["note"]
    assert data["stages"][0]["reached"] == 0


def test_funnel_service_caches_within_ttl(tmp_path):
    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        store.evaluation(symbol="XAUUSD", stage="signal", outcome="pass")
    finally:
        store.close()
    clock = {"now": NOW}
    service = FunnelService(JournalAdapter(tmp_path / "journal.sqlite"), None,
                            clock=lambda: clock["now"], cache_seconds=30)
    first = service.snapshot(hours=24)
    assert first["cached"] is False
    second = service.snapshot(hours=24)
    assert second["cached"] is True
    clock["now"] += 31
    third = service.snapshot(hours=24)
    assert third["cached"] is False


# ------------------------------------------------------------------- API surface
def _request(services) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(services=services)))


def test_funnel_endpoint_uses_injected_service():
    sentinel = {"available": True, "stages": []}
    calls = {}

    class Stub:
        def snapshot(self, hours=24, symbol=None):
            calls.update(hours=hours, symbol=symbol)
            return sentinel

    services = SimpleNamespace(funnel=Stub())
    assert funnel_endpoint(_request(services), hours=12, symbol="XAUUSD") is sentinel
    assert calls == {"hours": 12, "symbol": "XAUUSD"}


def test_funnel_endpoint_builds_a_fallback_service(tmp_path):
    store = StateStore(str(tmp_path / "journal.sqlite"))
    try:
        store.evaluation(symbol="XAUUSD", stage="signal", outcome="pass")
    finally:
        store.close()
    services = SimpleNamespace(
        funnel=None,
        journal=JournalAdapter(tmp_path / "journal.sqlite"),
        mt5=FakeMT5([_closed(close_time=time.time() - 60)]))
    data = funnel_endpoint(_request(services), hours=24, symbol="xauusd")
    assert data["symbol"] == "XAUUSD"
    assert data["evaluations"] == 1
    assert data["stages"][-1]["reached"] == 1
