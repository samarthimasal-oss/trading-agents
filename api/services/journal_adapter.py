"""Read-only access to the runner's journal (``agents.sqlite``).

The dashboard must never write this database.  Every query opens a short-lived
read-only connection so a missing or not-yet-created journal degrades to an
empty result instead of creating a file or blocking the runner.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote


class JournalAdapter:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ------------------------------------------------------------------ plumbing
    def _connect(self) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        uri = "file:" + quote(self.path.resolve().as_posix()) + "?mode=ro"
        try:
            db = sqlite3.connect(uri, uri=True, timeout=1.5)
        except sqlite3.Error:
            return None
        db.row_factory = sqlite3.Row
        return db

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        db = self._connect()
        if db is None:
            return []
        try:
            rows = db.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            db.close()

    @property
    def available(self) -> bool:
        return self.path.exists()

    # ---------------------------------------------------------------- decisions
    def decisions(self, limit: int = 100, symbol: str | None = None,
                  since: float | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._query(
            f"SELECT * FROM decisions {where} ORDER BY ts DESC LIMIT ?",
            tuple(params))

    def events(self, limit: int = 100, kind: str | None = None,
               symbol: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._query(
            f"SELECT * FROM events {where} ORDER BY ts DESC LIMIT ?",
            tuple(params))

    def equity(self, limit: int = 500) -> list[dict]:
        limit = max(1, min(int(limit), 5000))
        return self._query(
            "SELECT * FROM (SELECT * FROM equity_log ORDER BY ts DESC LIMIT ?) "
            "ORDER BY ts ASC", (limit,))

    def guardian(self) -> dict:
        rows = self._query(
            "SELECT day, day_start_equity, halted_day, halt_reason, halted_at, "
            "updated FROM guardian_state WHERE id=1 LIMIT 1")
        if not rows:
            return {
                "available": False,
                "reason": "guardian state has not been written yet",
                "day": None,
                "day_start_equity": None,
                "halted_day": None,
                "halt_reason": "",
                "halted_at": None,
                "updated": None,
            }
        row = dict(rows[0])
        row["available"] = True
        return row

    def stats(self) -> dict:
        decisions = self._query("SELECT COUNT(*) AS n, COALESCE(SUM(executed),0) AS e "
                               "FROM decisions")
        events = self._query("SELECT COUNT(*) AS n FROM events WHERE kind='ORDER_FILLED'")
        equity = self._query("SELECT COUNT(*) AS n FROM equity_log")
        return {
            "available": self.available,
            "decisions": int(decisions[0]["n"]) if decisions else 0,
            "executed": int(decisions[0]["e"]) if decisions else 0,
            "filled_orders": int(events[0]["n"]) if events else 0,
            "equity_points": int(equity[0]["n"]) if equity else 0,
        }

    def close(self) -> None:
        """No persistent connection is held; kept for the shared lifecycle."""
        return None


__all__ = ["JournalAdapter"]
