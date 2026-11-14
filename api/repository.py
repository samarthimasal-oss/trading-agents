"""Dashboard-owned SQLite state: audit trail, idempotency and backtest jobs.

The runner's ``agents.sqlite`` is read-only to the dashboard.  Keeping the
dashboard's own writes in a separate database means a dashboard bug cannot
corrupt the trading journal.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  payload_hash TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT 'ok',
  detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  result TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_jobs (
  id TEXT PRIMARY KEY,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  status TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}',
  pid INTEGER,
  result_path TEXT NOT NULL DEFAULT '',
  log_path TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


def _hash_payload(payload: Any) -> str:
    if payload is None:
        return ""
    try:
        raw = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DashboardStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------------ audit
    def audit(self, actor: str, action: str, *, target: str = "",
              payload: Any = None, result: str = "ok",
              detail: str = "") -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO audit (ts, actor, action, target, payload_hash, "
                "result, detail) VALUES (?,?,?,?,?,?,?)",
                (time.time(), str(actor), str(action), str(target),
                 _hash_payload(payload), str(result), str(detail)),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def recent_audit(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ idempotency
    def get_idempotency(self, key: str, action: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT action, result FROM idempotency WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        if row["action"] != action:
            raise ValueError(
                f"idempotency key {key!r} was already used for {row['action']!r}")
        try:
            return json.loads(row["result"])
        except (TypeError, ValueError):
            return None

    def save_idempotency(self, key: str, action: str, result: dict) -> None:
        raw = json.dumps(result, default=str)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO idempotency (key, action, result, created) "
                "VALUES (?,?,?,?)", (key, action, raw, time.time()))
            self._db.commit()

    # ------------------------------------------------------------- backtests
    def create_job(self, job_id: str, params: dict, *, status: str = "queued",
                   pid: Optional[int] = None, result_path: str = "",
                   log_path: str = "") -> dict:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO backtest_jobs (id, created, updated, status, params, "
                "pid, result_path, log_path, error) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, now, now, status, json.dumps(params, default=str),
                 pid, result_path, log_path, ""))
            self._db.commit()
        return self.get_job(job_id)

    def update_job(self, job_id: str, **fields) -> Optional[dict]:
        allowed = {"status", "pid", "result_path", "log_path", "error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_job(job_id)
        updates["updated"] = time.time()
        columns = ", ".join(f"{key}=?" for key in updates)
        with self._lock:
            self._db.execute(
                f"UPDATE backtest_jobs SET {columns} WHERE id=?",
                (*updates.values(), job_id))
            self._db.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM backtest_jobs WHERE id=?", (job_id,)).fetchone()
        return _job_row(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM backtest_jobs ORDER BY created DESC LIMIT ?",
                (max(1, min(int(limit), 200)),)).fetchall()
        return [_job_row(row) for row in rows]

    # ------------------------------------------------------------------ misc
    def ping(self) -> bool:
        with self._lock:
            self._db.execute("SELECT 1").fetchone()
        return True

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _job_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    try:
        data["params"] = json.loads(data.get("params") or "{}")
    except (TypeError, ValueError):
        data["params"] = {}
    return data


__all__ = ["DashboardStore"]
