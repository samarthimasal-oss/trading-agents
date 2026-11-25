"""SQLite journal for decisions/orders plus a rotating text log."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  ts REAL, symbol TEXT, action TEXT, direction INTEGER, volume REAL,
  sl REAL, tp REAL, strength REAL, confidence REAL,
  llm_action TEXT, llm_comment TEXT, reason TEXT, executed INTEGER
);
CREATE TABLE IF NOT EXISTS events (
  ts REAL, kind TEXT, symbol TEXT, detail TEXT, retcode INTEGER, price REAL, volume REAL
);
CREATE TABLE IF NOT EXISTS evaluations (
  ts REAL, bar_time REAL, symbol TEXT, stage TEXT, outcome TEXT,
  reason TEXT, direction INTEGER, confidence REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS equity_log (
  ts REAL, equity REAL, balance REAL, margin REAL, floating REAL
);
CREATE TABLE IF NOT EXISTS guardian_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  day TEXT,
  day_start_equity REAL,
  halted_day TEXT,
  halt_reason TEXT,
  halted_at REAL,
  updated REAL
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_evaluations_ts ON evaluations(ts);
CREATE INDEX IF NOT EXISTS idx_evaluations_stage ON evaluations(stage, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evaluations_bar
  ON evaluations(bar_time, symbol, stage);
"""


class StateStore:
    def __init__(self, db_file: str, log_file: str = None):
        os.makedirs(os.path.dirname(db_file) or ".", exist_ok=True)
        self.db = sqlite3.connect(db_file)
        self.db.executescript(_SCHEMA)
        self.db.commit()
        self.log_file = log_file
        if log_file:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)

    # ---------------------------------------------------------------- logging
    def attach_file_log(self, root_logger: logging.Logger, level=logging.INFO):
        if not self.log_file:
            return
        handler = logging.FileHandler(self.log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root_logger.addHandler(handler)
        root_logger.setLevel(level)

    # ---------------------------------------------------------------- writes
    def decision(self, *, symbol: str, action: str, direction: int, volume: float,
                 sl: float, tp: float, strength: float, confidence: float,
                 llm_action: str, llm_comment: str, reason: str, executed: bool):
        self.db.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), symbol, action, direction, volume, sl, tp, strength,
             confidence, llm_action, llm_comment, reason, 1 if executed else 0),
        )
        self.db.commit()

    def event(self, *, kind: str, symbol: str = "", detail: str = "",
              retcode: int = 0, price: float = 0.0, volume: float = 0.0):
        self.db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
            (time.time(), kind, symbol, detail, int(retcode), price, volume),
        )
        self.db.commit()

    def evaluation(self, *, stage: str, outcome: str, symbol: str = "",
                   reason: str = "", direction: int = 0, confidence: float = 0.0,
                   bar_time: float = 0.0, detail: str = ""):
        """Record one funnel stage transition for a symbol/bar.

        Exactly one row per stage per closed bar lets the dashboard count how
        many candidates reached each gate and why the rest stopped, without
        inferring anything from price data.
        """
        self.db.execute(
            "INSERT OR REPLACE INTO evaluations "
            "(ts, bar_time, symbol, stage, outcome, reason, direction, "
            " confidence, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), float(bar_time or 0.0), symbol, str(stage),
             str(outcome), str(reason or "")[:500], int(direction or 0),
             float(confidence or 0.0), str(detail or "")[:2000]),
        )
        self.db.commit()

    def equity(self, equity: float, balance: float, margin: float, floating: float):
        self.db.execute(
            "INSERT INTO equity_log VALUES (?,?,?,?,?)",
            (time.time(), equity, balance, margin, floating),
        )
        self.db.commit()

    # ---------------------------------------------------------------- reads
    def recent_decisions(self, limit: int = 20) -> list[sqlite3.Row]:
        cur = self.db.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,))
        cur.row_factory = sqlite3.Row
        return cur.fetchall()

    def stats(self) -> dict:
        cur = self.db.execute("SELECT COUNT(*), SUM(executed) FROM decisions")
        total, executed = cur.fetchone() or (0, 0)
        cur = self.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='ORDER_FILLED'")
        filled = cur.fetchone()[0]
        return {"decisions": total or 0, "executed": executed or 0, "filled_orders": filled}

    def peak_equity(self) -> float:
        cur = self.db.execute("SELECT MAX(equity) FROM equity_log")
        val = cur.fetchone()[0]
        return float(val) if val else 0.0

    def has_event(self, kind: str, detail_contains: str = "") -> bool:
        """True when an event of this kind (optionally matching detail) exists."""
        cur = self.db.execute(
            "SELECT 1 FROM events WHERE kind=? AND detail LIKE ? LIMIT 1",
            (kind, f"%{detail_contains}%"),
        )
        return cur.fetchone() is not None

    def evaluations(self, limit: int = 200, since: float | None = None,
                    symbol: str | None = None) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 5000)))
        cur = self.db.execute(
            f"SELECT * FROM evaluations {where} ORDER BY ts DESC, rowid DESC LIMIT ?",
            tuple(params))
        cur.row_factory = sqlite3.Row
        return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------ guardian
    def guardian_state(self) -> dict | None:
        """Return the persisted guardian snapshot, or None on a fresh DB."""
        cur = self.db.execute(
            "SELECT day, day_start_equity, halted_day, halt_reason, halted_at, updated "
            "FROM guardian_state WHERE id=1")
        row = cur.fetchone()
        if not row:
            return None
        return {
            "day": row[0],
            "day_start_equity": float(row[1]) if row[1] is not None else None,
            "halted_day": row[2],
            "halt_reason": row[3] or "",
            "halted_at": float(row[4]) if row[4] is not None else None,
            "updated": float(row[5]) if row[5] is not None else None,
        }

    def save_guardian_state(self, *, day: str, day_start_equity: float | None,
                            halted_day: str | None, halt_reason: str = "",
                            halted_at: float | None = None) -> None:
        """Atomically upsert the singleton guardian row."""
        self.db.execute(
            "INSERT INTO guardian_state "
            "(id, day, day_start_equity, halted_day, halt_reason, halted_at, updated) "
            "VALUES (1, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "day=excluded.day, day_start_equity=excluded.day_start_equity, "
            "halted_day=excluded.halted_day, halt_reason=excluded.halt_reason, "
            "halted_at=excluded.halted_at, updated=excluded.updated",
            (day, day_start_equity, halted_day, halt_reason, halted_at, time.time()),
        )
        self.db.commit()

    def close(self):
        self.db.close()
