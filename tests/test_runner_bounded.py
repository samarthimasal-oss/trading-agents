"""Bounded runner loop: ``--seconds`` and ``--once`` stop without a signal."""
from __future__ import annotations

import runner as runner_mod
from runner import TradingAgentRunner


def _bare_runner(poll: float = 5.0):
    obj = object.__new__(TradingAgentRunner)
    obj.cfg = {"trading": {"poll_seconds": poll}}
    obj.dry_run = True
    obj.symbols = []
    obj._board_state = {}
    calls = {"cycles": 0}

    def cycle():
        calls["cycles"] += 1
        return {"cycle": calls["cycles"]}

    obj.cycle = cycle
    return obj, calls


def test_run_forever_stops_at_the_deadline(monkeypatch):
    obj, calls = _bare_runner(poll=5.0)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(runner_mod.time, "time", lambda: clock["now"])

    def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(runner_mod.time, "sleep", fake_sleep)

    obj.run_forever(seconds=5.0)

    assert calls["cycles"] == 1
    assert clock["now"] == 1_005.0


def test_run_forever_without_a_deadline_runs_until_interrupt(monkeypatch):
    obj, calls = _bare_runner(poll=1.0)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(runner_mod.time, "time", lambda: clock["now"])

    def fake_sleep(seconds):
        clock["now"] += seconds
        if calls["cycles"] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(runner_mod.time, "sleep", fake_sleep)

    obj.run_forever(seconds=0.0)

    assert calls["cycles"] == 3


def test_cmd_run_once_executes_exactly_one_cycle(monkeypatch):
    import main as main_mod

    calls: dict = {}

    class FakeStore:
        def __init__(self, *args, **kwargs):
            pass

        def attach_file_log(self, *args, **kwargs):
            pass

        def close(self):
            calls["closed"] = True

    class FakeRunner:
        def __init__(self, cfg, store, dry_run=False):
            calls["dry_run"] = dry_run

        def start(self):
            calls["started"] = True

        def cycle(self):
            calls["cycles"] = calls.get("cycles", 0) + 1
            return {"ok": True}

        def run_forever(self, seconds=0.0):
            calls["run_forever"] = seconds

        def stop(self):
            calls["stopped"] = True

    monkeypatch.setattr(main_mod, "StateStore", FakeStore)
    monkeypatch.setattr(runner_mod, "TradingAgentRunner", FakeRunner)

    code = main_mod.cmd_run(
        {"logging": {"db_file": "unused.sqlite", "log_file": "unused.log"}},
        dry_run=True, once=True)

    assert code == 0
    assert calls == {"dry_run": True, "started": True, "cycles": 1,
                     "stopped": True, "closed": True}
