"""Backtest artifact index and walk-forward job manager.

Reading artifacts is implemented against existing ``docs/reports`` and
``state/backtests`` JSON files.  Jobs run the real research CLI
(``python main.py walkforward ...``) as a subprocess, one at a time, with the
output captured under ``state/backtests/logs`` and the result JSON written by
the CLI itself.  The API never blocks on a run; status is derived from the
dashboard's own SQLite ``backtest_jobs`` table plus a process liveness sweep so
a dashboard restart cannot orphan a job's status.
"""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from api.errors import APIError

log = logging.getLogger("api.backtests")

JOB_TIMEOUT_SECONDS = 2 * 3600
QUEUE_CAP = 5
STALE_QUEUE_SECONDS = 3600
LOG_TAIL_CHARS = 4000
ENGINES = ("trend", "breakout", "smc", "momentum", "mean_reversion")


def _pid_alive(pid: int | None) -> bool:
    """Existence check that never signals or terminates the target.

    ``os.kill(pid, 0)`` would *terminate* the process on Windows, so the
    liveness probe uses OpenProcess/GetExitCodeProcess there instead.
    """
    if not pid:
        return False
    pid = int(pid)
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                # Access denied means the process exists but is not ours.
                return bool(kernel32.GetLastError() == 5)
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:                                # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _metrics(block: Any) -> dict:
    if not isinstance(block, dict):
        return {"available": False, "reason": "missing result block"}
    metrics = block.get("metrics") if isinstance(block.get("metrics"), dict) else block
    if not metrics:
        return {"available": False, "reason": "result block has no metrics"}
    trades = metrics.get("trades")
    if isinstance(trades, list):
        trades = len(trades)
    return {
        "available": True,
        "trades": trades,
        "wins": metrics.get("wins"),
        "losses": metrics.get("losses"),
        "win_rate": metrics.get("win_rate"),
        "total_r": metrics.get("total_r"),
        "expectancy_r": metrics.get("expectancy_r"),
        "profit_factor": metrics.get("profit_factor"),
        "max_drawdown_r": metrics.get("max_drawdown_r"),
        "max_consecutive_losses": metrics.get("max_consecutive_losses"),
        "sharpe": metrics.get("sharpe"),
        "avg_mfe_r": metrics.get("avg_mfe_r"),
        "avg_mae_r": metrics.get("avg_mae_r"),
        "exit_reasons": metrics.get("exit_reasons") or {},
    }


def _gate_status(gate: dict) -> str:
    if gate.get("skipped"):
        return "skip"
    if "passed" in gate:
        return "pass" if gate.get("passed") else "fail"
    return str(gate.get("status") or "skip").upper().lower()


def _summary(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"name": path.name, "path": str(path), "available": False,
                "reason": f"unreadable artifact: {exc}"}
    if not isinstance(data, dict):
        return {"name": path.name, "path": str(path), "available": False,
                "reason": "unknown artifact shape"}
    gates = [gate for gate in (data.get("gates") or []) if isinstance(gate, dict)]
    gate_counts = {"pass": 0, "fail": 0, "skip": 0}
    for gate in gates:
        gate_counts[_gate_status(gate)] = gate_counts.get(_gate_status(gate), 0) + 1
    passed = data.get("passed")
    folds = []
    for fold in (data.get("folds") or []):
        if not isinstance(fold, dict):
            continue
        folds.append({
            "index": fold.get("index"),
            "test_start": fold.get("test_start"),
            "test_end": fold.get("test_end"),
            "metrics": _metrics(fold.get("metrics") if isinstance(fold.get("metrics"), dict)
                                else fold),
        })
    return {
        "name": path.name,
        "path": str(path),
        "available": True,
        "created": path.stat().st_mtime,
        "passed": passed if isinstance(passed, bool) else None,
        "data_note": data.get("data_note"),
        "oos": _metrics(data.get("oos")),
        "holdout": _metrics(data.get("holdout")),
        "folds": folds,
        "gates": gates,
        "gate_counts": gate_counts,
        "monte_carlo": data.get("monte_carlo"),
        "sensitivity": data.get("sensitivity"),
        "config": data.get("config"),
        "split": data.get("split"),
        "holdout_window": data.get("holdout_window"),
    }


class BacktestsService:
    def __init__(self, root: str | Path, store: Any,
                 config_getter: Callable[[], dict] | None = None,
                 spawner: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 timeout: float = JOB_TIMEOUT_SECONDS):
        self.root = Path(root)
        self.store = store
        self.config_getter = config_getter or (lambda: {})
        self.spawner = spawner or self._default_spawner
        self.clock = clock
        self.timeout = timeout
        self.artifact_dirs = [self.root / "docs" / "reports", self.root / "state" / "backtests"]
        self.log_dir = self.root / "state" / "backtests" / "logs"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._current_proc: Any = None
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._worker_loop,
                                        name="backtest-worker", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _default_spawner(argv: list[str], cwd: str, stdout: Any, stderr: Any):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.Popen(argv, cwd=cwd, stdout=stdout, stderr=stderr,
                                creationflags=flags)

    @staticmethod
    def _candidate(path: Path) -> bool:
        if path.name.startswith("walkforward_"):
            return True
        return path.parent.name == "backtests"

    def _allowed_symbols(self) -> set[str]:
        cfg = self.config_getter() or {}
        trading = cfg.get("trading") or {}
        allowed = {"XAUUSD"}
        symbol = str(trading.get("symbol") or "").upper().strip()
        if symbol:
            allowed.add(symbol)
        extra = trading.get("symbols")
        if isinstance(extra, str):
            allowed.update(part.strip().upper() for part in extra.split(",") if part.strip())
        elif isinstance(extra, (list, tuple)):
            allowed.update(str(part).strip().upper() for part in extra if str(part).strip())
        return allowed

    def _validate(self, params: dict) -> dict:
        clean = dict(params or {})
        symbol = str(clean.get("symbol") or "XAUUSD").upper().strip()
        if not (3 <= len(symbol) <= 12 and symbol.replace("/", "").isalnum()
                and symbol.replace("/", "").isascii()):
            raise APIError(422, "validation_error",
                           f"symbol {symbol!r} is not a valid MT5 symbol name")
        if symbol not in self._allowed_symbols():
            raise APIError(422, "validation_error",
                           f"symbol {symbol!r} is not on the dashboard allow-list",
                           {"allowed": sorted(self._allowed_symbols())})
        bars = int(clean.get("bars") or 12000)
        folds = int(clean.get("folds") or 4)
        holdout = float(clean.get("holdout") or 0.25)
        min_test_bars = int(clean.get("min_test_bars") or 200)
        mc_runs = int(clean.get("mc_runs") or 2000)
        seed = int(clean.get("seed") or 42)
        if not 3000 <= bars <= 30000:
            raise APIError(422, "validation_error", "bars must be between 3000 and 30000")
        if not 1 <= folds <= 8:
            raise APIError(422, "validation_error", "folds must be between 1 and 8")
        if not math.isfinite(holdout) or not 0.05 <= holdout <= 0.5:
            raise APIError(422, "validation_error", "holdout must be between 0.05 and 0.5")
        if not 50 <= min_test_bars <= 1000:
            raise APIError(422, "validation_error", "min_test_bars must be between 50 and 1000")
        if not 0 <= mc_runs <= 5000:
            raise APIError(422, "validation_error", "mc_runs must be between 0 and 5000")
        if not 1 <= seed <= 2**31 - 1:
            raise APIError(422, "validation_error", "seed must be a positive integer")
        only = clean.get("only") or None
        if only is not None:
            only = str(only).strip().lower()
            if only not in ENGINES:
                raise APIError(422, "validation_error",
                               f"only must be one of: {', '.join(ENGINES)}",
                               {"allowed": list(ENGINES)})
        return {
            "symbol": symbol,
            "bars": bars,
            "folds": folds,
            "holdout": holdout,
            "min_test_bars": min_test_bars,
            "mc_runs": mc_runs,
            "seed": seed,
            "only": only,
            "no_sensitivity": bool(clean.get("no_sensitivity")),
        }

    def _argv(self, params: dict, result_path: Path) -> list[str]:
        argv = [
            sys.executable, "main.py", "walkforward",
            "--symbol", params["symbol"],
            "--bars", str(params["bars"]),
            "--folds", str(params["folds"]),
            "--holdout", str(params["holdout"]),
            "--min-test-bars", str(params["min_test_bars"]),
            "--mc-runs", str(params["mc_runs"]),
            "--seed", str(params["seed"]),
            "--json", str(result_path),
        ]
        if params.get("only"):
            argv += ["--only", str(params["only"])]
        if params.get("no_sensitivity"):
            argv.append("--no-sensitivity")
        return argv

    def _log_tail(self, log_path: Any, limit: int = LOG_TAIL_CHARS) -> str:
        path = Path(log_path or "")
        if not path.is_absolute():
            path = self.root / path
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return data[-limit:]

    def _artifact_for(self, job: dict) -> dict | None:
        result_path = Path(job.get("result_path") or "")
        if not result_path.name:
            return None
        if not result_path.is_absolute():
            result_path = self.root / result_path
        if not result_path.exists():
            return None
        return _summary(result_path)

    # -------------------------------------------------------------- job control
    def start(self, params: dict | None = None, actor: str = "local") -> dict:
        clean = self._validate(params or {})
        self._sweep()
        active = [job for job in self.store.list_jobs(QUEUE_CAP + QUEUE_CAP)
                  if job.get("status") in ("queued", "running")]
        if len(active) >= QUEUE_CAP:
            raise APIError(
                429, "queue_full",
                f"the backtest queue is full ({len(active)} jobs pending); "
                "wait for a run to finish",
                {"queued_or_running": len(active), "cap": QUEUE_CAP})
        job_id = f"bt-{uuid.uuid4().hex[:12]}"
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.clock()))
        result_path = self.root / "state" / "backtests" / f"walkforward_{stamp}_{secrets.token_hex(3)}.json"
        log_path = self.log_dir / f"{job_id}.log"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        job = self.store.create_job(job_id, clean, status="queued",
                                    result_path=str(result_path), log_path=str(log_path))
        self.store.audit(str(actor), "backtest.start", target=job_id,
                         payload=clean, result="queued")
        self._wake.set()
        return job

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=2.0)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self._drain()
            except Exception:                            # noqa: BLE001
                log.exception("backtest worker loop failed")

    def _drain(self) -> None:
        while not self._stop.is_set():
            job = next((candidate for candidate in self.store.list_jobs(50)
                        if candidate.get("status") == "queued"), None)
            if job is None:
                return
            self._run_job(job)

    def _run_job(self, job: dict) -> None:
        job_id = str(job["id"])
        params = dict(job.get("params") or {})
        result_path = Path(job.get("result_path") or "")
        log_path = Path(job.get("log_path") or "")
        try:
            argv = self._argv(params, result_path)
        except Exception as exc:                         # noqa: BLE001
            self.store.update_job(job_id, status="failed", error=f"bad job params: {exc}")
            return
        self.store.update_job(job_id, status="running")
        try:
            handle = log_path.open("ab")
        except OSError as exc:
            self.store.update_job(job_id, status="failed", error=f"log file failed: {exc}")
            return
        try:
            with handle:
                try:
                    proc = self.spawner(argv, cwd=str(self.root),
                                        stdout=handle, stderr=subprocess.STDOUT)
                except Exception as exc:                 # noqa: BLE001
                    self.store.update_job(job_id, status="failed",
                                          error=f"failed to start walkforward: {exc}")
                    return
                with self._lock:
                    self._current_proc = proc
                self.store.update_job(job_id, pid=proc.pid)
                try:
                    code = proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = None
                    handle.write(b"\n[job killed] exceeded the 2h time limit\n")
                except Exception as exc:             # noqa: BLE001
                    self.store.update_job(job_id, status="failed",
                                          error=f"walkforward process failed: {exc}")
                    return
        finally:
            with self._lock:
                self._current_proc = None
        self._finalize(job_id, code, result_path, log_path)

    def _finalize(self, job_id: str, code: int | None,
                  result_path: Path, log_path: Path) -> None:
        if code == 0 and result_path.exists():
            self.store.update_job(job_id, status="done", error="")
            self.store.audit("worker", "backtest.finish", target=job_id, result="done")
            return
        tail = self._log_tail(log_path, 400).replace("\n", " | ").strip()
        if code == 0:
            error = "walkforward exited 0 without writing the artifact"
        elif code is None:
            error = "walkforward exceeded the 2h time limit and was killed"
        else:
            error = f"walkforward exited with code {code}"
        if tail:
            error = f"{error}; log: {tail[-280:]}"
        self.store.update_job(job_id, status="failed", error=error)
        self.store.audit("worker", "backtest.finish", target=job_id,
                         result="failed", detail=error[:280])

    def _sweep(self) -> None:
        """Finalise jobs orphaned by an API restart or a dead subprocess."""
        now = self.clock()
        for job in self.store.list_jobs(QUEUE_CAP * 4):
            status = job.get("status")
            if status == "queued":
                self._wake.set()
                created = float(job.get("created") or 0.0)
                if now - created > STALE_QUEUE_SECONDS:
                    self.store.update_job(str(job["id"]), status="failed",
                                          error="queued for over an hour without starting")
                continue
            if status != "running":
                continue
            result_path = Path(job.get("result_path") or "")
            if not result_path.name:
                continue
            if not result_path.is_absolute():
                result_path = self.root / result_path
            if result_path.exists():
                self._finalize(str(job["id"]), 0, result_path,
                               Path(job.get("log_path") or ""))
                continue
            if _pid_alive(job.get("pid")):
                continue
            log_path = Path(job.get("log_path") or "")
            tail = self._log_tail(log_path, 300).replace("\n", " | ").strip()
            error = ("the walkforward process is no longer running and left no "
                     "artifact (dashboard restart or process death)")
            if tail:
                error = f"{error}; log: {tail[-240:]}"
            self.store.update_job(str(job["id"]), status="failed", error=error)
            self.store.audit("worker", "backtest.finish", target=str(job["id"]),
                             result="failed", detail=error[:280])

    # -------------------------------------------------------------------- reads
    def list(self) -> dict:
        self._sweep()
        jobs = self.store.list_jobs(50)
        artifacts = []
        for directory in self.artifact_dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json"), reverse=True):
                if self._candidate(path):
                    artifacts.append(_summary(path))
        artifacts.sort(key=lambda item: item.get("created") or 0, reverse=True)
        queued = sum(1 for job in jobs if job.get("status") == "queued")
        running = sum(1 for job in jobs if job.get("status") == "running")
        return {
            "jobs": jobs,
            "queue": {"queued": queued, "running": running, "cap": QUEUE_CAP},
            "artifacts": artifacts,
            "available": True,
            "note": ("walk-forward treats the strategy rules as fixed; the "
                     "holdout is evaluated once and is not a tuning set. Jobs "
                     "run main.py walkforward with config.json — Strategy Desk "
                     "overrides are not applied."),
        }

    def get(self, job_id: str) -> dict:
        self._sweep()
        job = self.store.get_job(job_id)
        if job is None:
            raise APIError(404, "job_not_found", f"no backtest job {job_id!r}")
        return {
            **job,
            "artifact": self._artifact_for(job),
            "log_tail": self._log_tail(job.get("log_path")) or None,
        }

    def report(self, job_id: str) -> dict:
        self._sweep()
        job = self.store.get_job(job_id)
        if job is None:
            raise APIError(404, "job_not_found", f"no backtest job {job_id!r}")
        result_path = Path(job.get("result_path") or "")
        if not result_path.name:
            raise APIError(
                404, "report_not_available",
                "the job has not produced a result artifact yet",
                {"status": job.get("status")})
        if not result_path.is_absolute():
            result_path = self.root / result_path
        if not result_path.exists():
            raise APIError(
                404, "report_not_available",
                "the job has not produced a result artifact yet",
                {"status": job.get("status"), "log_path": job.get("log_path")})
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise APIError(500, "artifact_unreadable",
                           f"the artifact could not be parsed: {exc}") from exc
        if not isinstance(data, dict):
            raise APIError(500, "artifact_unreadable", "unknown artifact shape")
        return data

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            proc = self._current_proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:                            # noqa: BLE001
                log.debug("backtest process kill on close failed", exc_info=True)
        if self._worker.is_alive():
            self._worker.join(timeout=3.0)


__all__ = ["BacktestsService", "ENGINES", "QUEUE_CAP"]
