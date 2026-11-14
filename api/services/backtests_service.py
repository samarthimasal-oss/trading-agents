"""Backtest artifact index.

Reading is implemented now.  Starting jobs is a later slice and is deliberately
absent rather than a button with no effect.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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
    def __init__(self, root: str | Path, store: Any):
        self.root = Path(root)
        self.store = store
        self.artifact_dirs = [self.root / "docs" / "reports", self.root / "state" / "backtests"]

    @staticmethod
    def _candidate(path: Path) -> bool:
        if path.name.startswith("walkforward_"):
            return True
        return path.parent.name == "backtests"

    def list(self) -> dict:
        artifacts = []
        for directory in self.artifact_dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json"), reverse=True):
                if self._candidate(path):
                    artifacts.append(_summary(path))
        artifacts.sort(key=lambda item: item.get("created") or 0, reverse=True)
        return {
            "jobs": self.store.list_jobs(50),
            "artifacts": artifacts,
            "available": True,
            "note": ("walk-forward treats the strategy rules as fixed; the "
                     "holdout is evaluated once and is not a tuning set"),
        }


__all__ = ["BacktestsService"]
