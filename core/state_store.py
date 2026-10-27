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
CREATE TABLE IF NOT EXISTS equity_log (
  ts REAL, equity REAL, balance REAL, margin REAL, floating REAL
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
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

    def close(self):
        self.db.close()
