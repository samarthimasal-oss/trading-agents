"""Process runner used by the dashboard.

Only DRY-RUN starts are exposed.  Live trading is intentionally not reachable
from this service: the walk-forward gate failed and no engine has been
promoted.  The class keeps the legacy pid-file protocol so the Flask
dashboard and a manually started runner remain visible.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from api.errors import APIError


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class RunnerService:
    def __init__(self, root: str | Path, pid_file: str | Path,
                 log_file: str | Path, python: str | None = None):
        self.root = Path(root)
        self.pid_file = Path(pid_file)
        self.log_file = Path(log_file)
        self.python = python or sys.executable
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        with self._lock:
            state: dict = {"mode": "stopped", "pid": None, "started": None,
                           "stale": False}
            if self.pid_file.exists():
                try:
                    loaded = json.loads(self.pid_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        state.update(loaded)
                except (OSError, ValueError):
                    state["stale"] = True
            pid = state.get("pid")
            if pid and not pid_alive(int(pid)):
                state.update({"mode": "stopped", "pid": None, "started": None,
                              "stale": True})
                try:
                    self.pid_file.unlink()
                except OSError:
                    pass
            return state

    # ------------------------------------------------------------------- start
    def start(self, dry_run: bool = True) -> dict:
        if not dry_run:
            raise APIError(403, "live_disabled",
                           "live runner start is disabled until the walk-forward "
                           "gate passes and an engine is promoted")
        with self._lock:
            current = self.status()
            if current.get("mode") not in (None, "stopped") or current.get("pid"):
                raise APIError(409, "runner_already_running",
                               "a runner is already active",
                               {"mode": current.get("mode"), "pid": current.get("pid")})
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            cmd = [self.python, "-u", str(self.root / "main.py"), "run", "--dry-run"]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            with open(self.log_file, "ab") as log_handle:
                proc = subprocess.Popen(
                    cmd, cwd=str(self.root), stdout=log_handle,
                    stderr=subprocess.STDOUT, creationflags=creationflags)
            payload = {"pid": int(proc.pid), "mode": "dry-run",
                       "started": time.time()}
            tmp = self.pid_file.with_suffix(self.pid_file.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.pid_file)
            return self.status()

    # -------------------------------------------------------------------- stop
    def stop(self) -> dict:
        with self._lock:
            current = self.status()
            pid: Optional[int] = current.get("pid")
            if not pid:
                return current
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=20)
            else:
                try:
                    os.kill(int(pid), 15)
                except OSError:
                    pass
            try:
                self.pid_file.unlink()
            except OSError:
                pass
            return {"mode": "stopped", "pid": None, "started": None, "stale": False,
                    "stopped_pid": pid}


__all__ = ["RunnerService", "pid_alive"]
