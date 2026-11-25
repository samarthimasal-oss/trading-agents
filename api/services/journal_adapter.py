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

    # -------------------------------------------------------------- funnel data
    @staticmethod
    def _funnel_where(since: float | None, symbol: str | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        return (f"WHERE {' AND '.join(clauses)}" if clauses else "", params)

    def evaluations(self, limit: int = 200, since: float | None = None,
                    symbol: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 2000))
        where, params = self._funnel_where(since, symbol)
        params.append(limit)
        return self._query(
            f"SELECT * FROM evaluations {where} ORDER BY ts DESC, rowid DESC "
            f"LIMIT ?", tuple(params))

    def event_counts(self, since: float | None = None) -> dict:
        clauses, params = [], []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(
            f"SELECT kind, COUNT(*) AS n FROM events {where} GROUP BY kind",
            tuple(params))
        return {str(row["kind"]): int(row["n"]) for row in rows}

    def funnel(self, since: float | None = None,
               symbol: str | None = None) -> dict:
        """Aggregate stage rows and rejection reasons (read-only)."""
        where, params = self._funnel_where(since, symbol)
        rows = self._query(
            f"SELECT stage, outcome, COUNT(*) AS n FROM evaluations {where} "
            f"GROUP BY stage, outcome", tuple(params))
        reasons = self._query(
            f"SELECT stage, reason, COUNT(*) AS n FROM evaluations {where} "
            f"{'AND' if where else 'WHERE'} outcome IN ('reject','veto','loss') "
            f"AND reason <> '' GROUP BY stage, reason ORDER BY n DESC LIMIT 24",
            tuple(params))
        by_symbol = self._query(
            f"SELECT symbol, stage, outcome, COUNT(*) AS n FROM evaluations "
            f"{where} GROUP BY symbol, stage, outcome", tuple(params))
        latest = self._query(
            f"SELECT MAX(ts) AS latest, COUNT(*) AS n FROM evaluations {where}",
            tuple(params))
        return {
            "available": self.available,
            "rows": [{"stage": row["stage"], "outcome": row["outcome"],
                      "count": int(row["n"])} for row in rows],
            "reasons": [{"stage": row["stage"], "reason": row["reason"],
                         "count": int(row["n"])} for row in reasons],
            "by_symbol": [{"symbol": row["symbol"], "stage": row["stage"],
                           "outcome": row["outcome"], "count": int(row["n"])}
                          for row in by_symbol],
            "latest_ts": (float(latest[0]["latest"])
                          if latest and latest[0]["latest"] is not None else None),
            "evaluations": int(latest[0]["n"]) if latest else 0,
        }

    def close(self) -> None:
        """No persistent connection is held; kept for the shared lifecycle."""
        return None


__all__ = ["JournalAdapter"]
