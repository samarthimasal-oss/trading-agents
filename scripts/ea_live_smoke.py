"""Supervised PAPER-mode smoke test for the Python <-> EA bridge.

This is the repeatable version of the 2026-11-11 live smoke test. It:

1. starts the loopback bridge and waits for the EA;
2. sends one PAPER intent and expects a ``dry_run`` ack;
3. clears only the Python ack cache, resends the same key, and expects the
   EA's persisted idempotency cache to answer ``duplicate``;
4. restarts the bridge, waits for the EA to reconnect, and reconciles.

It never sends an order. It refuses to continue unless the EA reports
``mode=PAPER`` and ``dry_run=true`` in its ``hello``.

Prerequisites: MT5 is running with ``TradingAgentsEA`` attached in PAPER mode,
the host-only ``127.0.0.1`` WebRequest entry is active, the token file exists,
and no other bridge is listening on the port.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.ea_client import EAOrderBridge
from execution.ea_protocol import Intent, idempotency_key, load_token


def _log(message: str) -> None:
    print(message, flush=True)


def _start_bridge(token: str, port: int, retries: int = 5) -> EAOrderBridge:
    last: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        bridge = EAOrderBridge(token, port=port, ack_timeout=15)
        try:
            bridge.start()
            return bridge
        except OSError as exc:
            last = exc
            _log(f"bind retry {attempt}/{retries}: {exc}")
            time.sleep(2)
    raise SystemExit(f"could not bind bridge on port {port}: {last}")


def _require_paper(bridge: EAOrderBridge) -> dict:
    hello = bridge.hello
    if hello.get("mode") != "PAPER" or hello.get("dry_run") is not True:
        raise SystemExit(
            "refusing to run: EA is not in PAPER/dry-run mode; "
            f"hello={json.dumps(hello, sort_keys=True)}"
        )
    return hello


def _forget_python_ack(bridge: EAOrderBridge, key: str) -> None:
    # The EA's seen-key cache is the safety layer under test. Clearing only
    # the Python cache forces the EA to answer the second send.
    with bridge._lock:
        bridge._known.pop(key, None)
        bridge._pending.pop(key, None)
        bridge._pending_by_msg.clear()
        bridge._unknown_keys.discard(key)


def _price_from_mt5(symbol: str) -> float:
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise SystemExit(
            "MetaTrader5 package is required unless --price is supplied"
        ) from exc
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise SystemExit(f"no tick for {symbol}: {mt5.last_error()}")
        return float(tick.ask)
    finally:
        mt5.shutdown()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the supervised PAPER-mode Python/EA bridge smoke test."
    )
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--price", type=float, default=0.0,
                        help="override the MT5 ask price")
    parser.add_argument("--sl-distance", type=float, default=5.0)
    parser.add_argument("--tp-distance", type=float, default=8.0)
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="seconds to wait for the EA to connect")
    args = parser.parse_args(argv)

    if args.sl_distance <= 0 or args.tp_distance <= 0:
        raise SystemExit("--sl-distance and --tp-distance must be positive")

    token = load_token()
    price = args.price if args.price > 0 else _price_from_mt5(args.symbol)
    sl = round(price - args.sl_distance, 2)
    tp = round(price + args.tp_distance, 2)
    key = idempotency_key("trend", args.symbol, int(time.time()), 1)
    intent = Intent(
        idempotency_key=key,
        symbol=args.symbol,
        direction=1,
        volume=0.0,
        sl=sl,
        tp=tp,
        strategy="trend",
        comment="TA:smoke",
        risk_percent=0.5,
        max_slippage_points=30,
        expires_at=time.time() + 300,
    )
    _log(f"intent {key[:16]} price {price} sl {sl} tp {tp}")

    bridge1: Optional[EAOrderBridge] = None
    bridge2: Optional[EAOrderBridge] = None
    try:
        bridge1 = _start_bridge(token, args.port)
        _log(f"bridge1 listening {bridge1.bound_port}")
        if not bridge1.wait_connected(args.timeout):
            raise SystemExit("FAIL: EA did not connect to bridge1")
        _log("bridge1 hello " + json.dumps(_require_paper(bridge1), sort_keys=True))

        ack1 = bridge1.send_intent(intent, timeout=20)
        if ack1 is None:
            raise SystemExit("FAIL: no ack for the first intent")
        _log(f"ACK1 {ack1.status} ticket {ack1.ticket} retcode {ack1.retcode} "
             f"name {ack1.retcode_name} message {ack1.message}")
        if ack1.status == "rejected" and "outside" in ack1.message.lower():
            _log("SKIP: the EA rejected the intent because the market is outside")
            _log("      its allowed sessions (London/overlap/New York).")
            _log("      Rerun during an allowed session.")
            return 2
        if ack1.status != "dry_run":
            raise SystemExit(f"FAIL: expected dry_run, got {ack1.status}")

        _forget_python_ack(bridge1, key)
        ack2 = bridge1.send_intent(intent, timeout=20)
        if ack2 is None:
            raise SystemExit("FAIL: no ack for the duplicate intent")
        _log(f"ACK2 {ack2.status} ticket {ack2.ticket} retcode {ack2.retcode} "
             f"name {ack2.retcode_name} message {ack2.message}")
        if ack2.status != "duplicate":
            raise SystemExit(f"FAIL: expected duplicate, got {ack2.status}")

        bridge1.stop()
        bridge1 = None
        time.sleep(2)

        bridge2 = _start_bridge(token, args.port)
        _log(f"bridge2 listening {bridge2.bound_port}")
        if not bridge2.wait_connected(args.timeout):
            raise SystemExit("FAIL: EA did not reconnect to bridge2")
        _log("bridge2 hello " + json.dumps(_require_paper(bridge2), sort_keys=True))

        recon = bridge2.reconcile(timeout=20)
        _log("RECONCILE " + json.dumps(recon, sort_keys=True)[:3000])
        if recon.get("mismatches") or recon.get("unknown_keys"):
            raise SystemExit("FAIL: reconciliation reported mismatches or unknown keys")
        _log("STATUS " + json.dumps(bridge2.status(), sort_keys=True)[:1000])
        _log("SMOKE PASS")
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        _log(f"SMOKE FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        for bridge in (bridge1, bridge2):
            if bridge is not None:
                try:
                    bridge.stop()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
