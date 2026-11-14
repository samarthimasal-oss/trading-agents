"""Assemble the read-only dashboard snapshot used by the UI and WebSocket."""
from __future__ import annotations

import time
from typing import Any, Callable

from api.services import Services


def _fallback(reason: str, **extra: Any) -> dict:
    return {"available": False, "reason": reason, **extra}


def _safe(fn: Callable[[], Any], label: str) -> Any:
    try:
        return fn()
    except Exception as exc:                            # noqa: BLE001
        return _fallback(f"{label} unavailable: {exc}")


def _add_available(payload: Any) -> Any:
    if isinstance(payload, dict) and "available" not in payload:
        return {**payload, "available": True}
    return payload


def _magic(services: Services) -> Any:
    try:
        return (services.config.raw().get("trading") or {}).get("magic")
    except Exception:                                    # noqa: BLE001
        return None


def build_snapshot(services: Services) -> dict:
    ts = services.clock()
    magic = _magic(services)

    account = _add_available(_safe(services.mt5.account, "MT5 account"))
    positions = _safe(lambda: services.mt5.positions(magic=magic), "positions")
    orders = _safe(lambda: services.mt5.orders(magic=magic), "orders")
    runner = _safe(services.runner.status, "runner")
    risk = _safe(services.risk.snapshot, "risk snapshot")
    bridge = _safe(services.bridge_check, "EA bridge")
    journal_stats = _safe(services.journal.stats, "journal")

    return {
        "type": "snapshot",
        "ts": ts,
        "account": account,
        "positions": positions if isinstance(positions, list) else [],
        "positions_count": len(positions) if isinstance(positions, list) else None,
        "orders_count": len(orders) if isinstance(orders, list) else None,
        "runner": runner,
        "risk": risk,
        "bridge": bridge,
        "journal": journal_stats,
        "health": {
            "bridge": bool(bridge.get("listening")) if isinstance(bridge, dict) else False,
            "runner": (runner or {}).get("mode") if isinstance(runner, dict) else None,
            "account": bool(isinstance(account, dict) and account.get("available")),
        },
    }


def build_overview(services: Services) -> dict:
    snapshot = build_snapshot(services)
    committee = _safe(services.committee.snapshot, "committee")
    decisions = _safe(lambda: services.journal.decisions(limit=12), "decisions")
    events = _safe(lambda: services.journal.events(limit=12), "events")
    equity = _safe(lambda: services.journal.equity(limit=240), "equity")
    strategies = _safe(services.strategies.list, "strategies")
    return {
        **snapshot,
        "committee": committee,
        "decisions": decisions if isinstance(decisions, list) else [],
        "events": events if isinstance(events, list) else [],
        "equity": equity if isinstance(equity, list) else [],
        "strategies": strategies,
        "generated_at": time.time(),
    }
