"""Backtesting job manager: validation, queue, lifecycle and artifacts."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from api.errors import APIError
from api.repository import DashboardStore
from api.services.backtests_service import BacktestsService, QUEUE_CAP


class FakeProc:
    def __init__(self, artifact: Path | None, code: int = 0, pid: int | None = None):
        self.artifact = artifact
        self.code = code
        self.pid = pid if pid is not None else os.getpid()
        self.killed = False
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.code == 0 and self.artifact is not None:
            self.artifact.parent.mkdir(parents=True, exist_ok=True)
            self.artifact.write_text(json.dumps(_artifact_body()),
                                     encoding="utf-8")
        return self.code

    def kill(self):
        self.killed = True


def _artifact_body(passed=False):
    return {
        "passed": passed,
        "data_note": "MT5 XAUUSD M15 (12000 bars)",
        "oos": {"trades": 21, "wins": 9, "losses": 12, "win_rate": 42.9,
                "total_r": -1.2, "expectancy_r": -0.06, "profit_factor": 0.8,
                "max_drawdown_r": 5.5},
        "holdout": {"trades": 5, "total_r": -0.4},
        "folds": [{"index": 0, "test_start": "t0", "test_end": "t1",
                   "metrics": {"trades": 4}}],
        "gates": [{"name": "min_trades", "passed": False},
                  {"name": "mc_dd95", "skipped": True}],
        "monte_carlo": {"runs": 2000},
        "sensitivity": [],
        "config": {},
        "split": {},
        "holdout_window": {},
    }


class Spawner:
    def __init__(self, artifact_for=lambda argv: None, code=0):
        self.calls: list[dict] = []
        self.artifact_for = artifact_for
        self.code = code

    def __call__(self, argv, cwd=None, stdout=None, stderr=None):
        self.calls.append({"argv": list(argv), "cwd": cwd})
        artifact = self.artifact_for(argv)
        return FakeProc(artifact, code=self.code)


@pytest.fixture()
def store(tmp_path):
    db = DashboardStore(tmp_path / "state" / "dashboard.sqlite")
    yield db
    db.close()


@pytest.fixture()
def service(tmp_path, store):
    config = {"trading": {"symbol": "XAUUSD", "symbols": ["EURUSD", "GBPUSD"]}}
    svc = BacktestsService(tmp_path, store, config_getter=lambda: config)
    yield svc
    svc.close()


def wait_for(store, job_id, statuses, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        if job and job["status"] in statuses:
            return job
        time.sleep(0.02)
    return store.get_job(job_id)


def test_start_validates_symbol_against_allow_list(service):
    with pytest.raises(APIError) as exc:
        service.start({"symbol": "US500"})
    assert exc.value.status_code == 422
    assert "allow-list" in exc.value.message
    with pytest.raises(APIError):
        service.start({"symbol": "XAUUSD", "bars": 100})
    with pytest.raises(APIError):
        service.start({"symbol": "XAUUSD", "folds": 99})
    with pytest.raises(APIError):
        service.start({"symbol": "XAUUSD", "only": "moonshot"})


def test_job_runs_to_done_and_lists_artifact(tmp_path, store, service):
    captured = {}

    def artifact_for(argv):
        captured["argv"] = argv
        # the --json path the service asked the CLI to write
        return Path(argv[argv.index("--json") + 1])

    spawner = Spawner(artifact_for=artifact_for)
    service.spawner = spawner
    job = service.start({"symbol": "XAUUSD", "bars": 9000})
    assert job["id"].startswith("bt-")
    final = wait_for(store, job["id"], {"done", "failed"})
    assert final["status"] == "done", final
    assert "--only" not in captured["argv"]
    assert captured["argv"][captured["argv"].index("--symbol") + 1] == "XAUUSD"
    assert captured["argv"][captured["argv"].index("--bars") + 1] == "9000"

    listing = service.list()
    assert listing["available"] is True
    assert any(a["available"] and a["name"].endswith(".json")
               for a in listing["artifacts"])
    artifact = listing["artifacts"][0]
    assert artifact["gate_counts"] == {"pass": 0, "fail": 1, "skip": 1}
    assert artifact["oos"]["trades"] == 21
    assert listing["queue"]["running"] == 0

    detail = service.get(job["id"])
    assert detail["artifact"] is not None
    assert detail["artifact"]["oos"]["total_r"] == pytest.approx(-1.2)

    report = service.report(job["id"])
    assert report["passed"] is False
    assert report["data_note"].startswith("MT5 XAUUSD")


def test_nonzero_exit_is_recorded_as_failed(tmp_path, store):
    config = {"trading": {"symbol": "XAUUSD"}}
    service = BacktestsService(tmp_path, store, config_getter=lambda: config,
                               spawner=Spawner(code=1))
    try:
        job = service.start({"symbol": "XAUUSD"})
        final = wait_for(store, job["id"], {"done", "failed"})
        assert final["status"] == "failed"
        assert "exited with code 1" in final["error"]
    finally:
        service.close()


def test_successful_exit_without_artifact_fails(tmp_path, store):
    config = {"trading": {"symbol": "XAUUSD"}}
    service = BacktestsService(tmp_path, store, config_getter=lambda: config,
                               spawner=Spawner(code=0))
    try:
        job = service.start({"symbol": "XAUUSD"})
        final = wait_for(store, job["id"], {"done", "failed"})
        assert final["status"] == "failed"
        assert "without writing the artifact" in final["error"]
    finally:
        service.close()


def test_queue_cap_blocks_new_jobs(store, tmp_path):
    config = {"trading": {"symbol": "XAUUSD"}}
    service = BacktestsService(tmp_path, store, config_getter=lambda: config)
    try:
        for index in range(QUEUE_CAP):
            store.create_job(f"busy-{index}", {}, status="running", pid=os.getpid())
        with pytest.raises(APIError) as exc:
            service.start({"symbol": "XAUUSD"})
        assert exc.value.status_code == 429
        assert "queue is full" in exc.value.message
    finally:
        service.close()


def test_orphaned_running_job_is_finalized_by_sweep(store, tmp_path):
    config = {"trading": {"symbol": "XAUUSD"}}
    service = BacktestsService(tmp_path, store, config_getter=lambda: config)
    try:
        store.create_job("orphan", {"symbol": "XAUUSD"}, status="running",
                         pid=999_999_999,
                         result_path=str(tmp_path / "state" / "backtests" / "missing.json"),
                         log_path=str(tmp_path / "state" / "backtests" / "logs" / "orphan.log"))
        listing = service.list()
        job = next(j for j in listing["jobs"] if j["id"] == "orphan")
        assert job["status"] == "failed"
        assert "no longer running" in job["error"]
    finally:
        service.close()


def test_report_missing_artifact_is_a_structured_error(tmp_path, store):
    config = {"trading": {"symbol": "XAUUSD"}}
    service = BacktestsService(tmp_path, store, config_getter=lambda: config)
    try:
        job = service.start({"symbol": "XAUUSD"})
        with pytest.raises(APIError) as exc:
            service.report(job["id"])
        assert exc.value.status_code == 404
        assert exc.value.code == "report_not_available"
        with pytest.raises(APIError) as exc:
            service.get("does-not-exist")
        assert exc.value.status_code == 404
        assert exc.value.code == "job_not_found"
    finally:
        service.close()


def test_start_audits_the_request(store, tmp_path):
    config = {"trading": {"symbol": "XAUUSD"}}
    service = BacktestsService(tmp_path, store, config_getter=lambda: config)
    try:
        job = service.start({"symbol": "XAUUSD"}, actor="tester")
        rows = store.recent_audit(10)
        assert any(r["action"] == "backtest.start" and r["target"] == job["id"]
                   and r["actor"] == "tester" for r in rows)
    finally:
        service.close()
