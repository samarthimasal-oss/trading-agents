"""Kronos accuracy scorecard: grade each forecast against what price did next.

Every time a bar closes on a timeframe, the runner hands the forecast that was
made from the *previous* bar (plus that bar's close) to :func:`grade`. The
record answers one question: did the ensemble's direction call match the next
bar? Over time this gives a live hit-rate / error scorecard per timeframe,
which the dashboard renders and which tells you whether Kronos is currently
earning its vote weight.

Storage is a small JSON file under ``state/`` (atomic replace, capped size),
so it survives runner restarts and is safe to read from the dashboard process.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("kronos_score")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCORE_PATH = os.path.join(_ROOT, "state", "kronos_score.json")

_LOCK = threading.Lock()
_MAX_RECORDS = 500
_MAX_RECENT = 12


# --------------------------------------------------------------------- io
def _load() -> dict:
    try:
        with open(SCORE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:                          # noqa: BLE001
        log.warning("Could not read %s: %s", SCORE_PATH, exc)
    return {"records": []}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(SCORE_PATH), exist_ok=True)
    tmp = SCORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, SCORE_PATH)


# ------------------------------------------------------------------ grading
def grade(payload: dict, symbol: str, timeframe: str,
          outcome_close: float, outcome_ts: float) -> dict | None:
    """Record one graded forecast. Returns the stored record (or None).

    ``payload`` is what :func:`agents.kronos_agent.forecast` returned when the
    previous bar closed; ``outcome_close`` is the close of the bar that just
    finished. Duplicate (symbol, tf, base_ts) records are ignored, which makes
    it safe to call from every poll.
    """
    if not payload or not payload.get("base_ts") or not payload.get("base_close"):
        return None
    base_ts = float(payload["base_ts"])
    base_close = float(payload["base_close"])
    if not outcome_close or base_close <= 0 or float(outcome_ts) <= base_ts:
        return None

    realized_pct = (float(outcome_close) - base_close) / base_close * 100.0
    direction = int(payload.get("dir", 0) or 0)
    pred_next = float(payload.get("move_next", 0.0) or 0.0)
    pred_next_pct = pred_next / base_close * 100.0
    # a neutral call (tie / OOD) is not graded as a hit or a miss
    hit = None if direction == 0 else int((direction > 0) == (realized_pct > 0))

    rec = {
        "symbol": symbol,
        "tf": str(timeframe).upper(),
        "base_ts": base_ts,
        "base_close": base_close,
        "outcome_ts": float(outcome_ts),
        "outcome_close": round(float(outcome_close), 8),
        "dir": direction,
        "runs": int(payload.get("runs", 1) or 1),
        "agreement": float(payload.get("agreement", 1.0) or 1.0),
        "pred_next_pct": round(pred_next_pct, 5),
        "realized_pct": round(realized_pct, 5),
        "error_pct": round(abs(realized_pct - pred_next_pct), 5),
        "hit": hit,
        "graded_at": time.time(),
    }

    key = (rec["symbol"], rec["tf"], base_ts)
    with _LOCK:
        data = _load()
        records = data["records"]
        if any((r.get("symbol"), r.get("tf"), r.get("base_ts")) == key
               for r in records[-_MAX_RECORDS:]):
            return None
        records.append(rec)
        if len(records) > _MAX_RECORDS:
            del records[: len(records) - _MAX_RECORDS]
        data["updated"] = time.time()
        _save(data)
    return rec


# ------------------------------------------------------------------ summary
def _stats(records: list[dict]) -> dict:
    graded = [r for r in records if r.get("hit") is not None]
    if not graded:
        return {"graded": 0, "hits": 0, "hit_rate": None,
                "avg_realized_pct": None, "avg_error_pct": None}
    hits = sum(int(r["hit"]) for r in graded)
    return {
        "graded": len(graded),
        "hits": hits,
        "hit_rate": round(hits / len(graded), 3),
        "avg_realized_pct": round(
            sum(abs(float(r.get("realized_pct", 0.0))) for r in graded) / len(graded), 4),
        "avg_error_pct": round(
            sum(float(r.get("error_pct", 0.0)) for r in graded) / len(graded), 4),
    }


def summary() -> dict:
    """Dashboard-ready aggregate: overall, last-20 form, per-timeframe split."""
    data = _load()
    records = data.get("records", [])
    out = _stats(records)
    recent = records[-20:]
    last = _stats(recent)
    out["recent_hit_rate"] = last["hit_rate"]
    out["recent_graded"] = last["graded"]

    by_tf: dict[str, dict] = {}
    for rec in records:
        by_tf.setdefault(str(rec.get("tf", "?")).upper(), []).append(rec)
    out["by_tf"] = {tf: _stats(rows) for tf, rows in sorted(by_tf.items())}

    # did agreement actually predict accuracy? split graded ensemble calls
    ens = [r for r in records if int(r.get("runs", 1) or 1) > 1 and r.get("hit") is not None]
    if ens:
        high = [r for r in ens if float(r.get("agreement", 0)) >= 0.99]
        split = [r for r in ens if float(r.get("agreement", 0)) < 0.99]
        out["agreement_split"] = {"unanimous": _stats(high), "split": _stats(split)}
    else:
        out["agreement_split"] = {}

    out["recent"] = [
        {"symbol": r.get("symbol"), "tf": r.get("tf"),
         "dir": r.get("dir"), "hit": r.get("hit"),
         "realized_pct": r.get("realized_pct"),
         "agreement": r.get("agreement"), "runs": r.get("runs"),
         "graded_at": r.get("graded_at")}
        for r in records[-_MAX_RECENT:][::-1]
    ]
    out["updated"] = data.get("updated")
    return out
