"""
TradingAgents for MetaTrader 5
==============================
Multi-agent pipeline: quant agents -> AI committee (Gemini) -> risk agent ->
executor + guardian, all operating on your MT5 account.

Usage:
  python main.py check                 # verify MT5 connectivity + config
  python main.py backtest             # replay history on your symbol
  python main.py run --dry-run        # live pipeline, no real orders
  python main.py run                  # LIVE trading on MT5 (careful!)
  python main.py status               # account + bot positions + journal
  python main.py strategies --csv x   # five-engine research report
  python main.py walkforward --bars 12000  # walk-forward + untouched holdout
  python main.py ea-token             # create shared EA token (loopback bridge)
  python main.py ea-server            # listen for the native MQL5 EA
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.state_store import StateStore  # noqa: E402
from core.config_schema import ConfigError, validate_config  # noqa: E402
from core.quotes import assess_quote, spread_limit_for  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    try:
        validate_config(cfg)
    except ConfigError as exc:
        print("CONFIGURATION ERROR", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    return cfg


def cmd_check(cfg: dict) -> int:
    print("=" * 60)
    print("TradingAgents :: system check")
    print("=" * 60)

    # 1. MetaTrader5 package
    try:
        import MetaTrader5 as mt5  # noqa
        print("[1/4] MetaTrader5 python package ....... OK")
    except ImportError:
        print("[1/4] MetaTrader5 python package ....... MISSING")
        print("      Fix: pip install MetaTrader5")
        return 1

    # 2. terminal / connection
    failure = None
    try:
        from core.mt5_client import MT5Client, MT5Error
        client = MT5Client(cfg["mt5"])
        client.connect()
        print("[2/4] MT5 terminal connection .......... OK")
    except Exception as exc:
        failure = exc
        print("[2/4] MT5 terminal connection .......... FAILED")
        print(f"      {exc}")
        print("      Notes: the MT5 terminal must be INSTALLED and RUNNING")
        print("      (logged in). Optionally set terminal_path/login/password")
        print("      server in config.json.")
        print("[3/4] account ......................... SKIPPED")
        print("[4/4] LLM key ......................... see below")
        llm_key_check(cfg)
        return 1

    # 3. account + symbols
    try:
        raw = mt5.account_info()
        print(f"[3/4] account {raw.login} equity={raw.equity:.2f} "
              f"balance={raw.balance:.2f} margin_level={raw.margin_level:.0f}")
        print("      account trade mode:", {0: "demo", 1: "contest",
                                          2: "real"}.get(raw.trade_mode, raw.trade_mode))
        syms = cfg["trading"].get("symbols") or [cfg["trading"]["symbol"]]
        resolved_names = []
        quarantined = []
        for sym in syms:
            resolved = client.resolve_symbol(sym)
            info = client.prepare(resolved)
            t = client.tick(resolved)
            q = assess_quote(
                t.bid, t.ask, info.point,
                max_spread_points=spread_limit_for(sym, cfg["risk"]),
            )
            status = "OK" if q.valid else f"QUARANTINED ({q.reason})"
            print(f"      {sym} -> {resolved:12s} bid={t.bid} ask={t.ask} "
                  f"spread={q.spread_points:.1f}pts  {status}")
            resolved_names.append(resolved)
            if not q.valid:
                quarantined.append(sym)
        if quarantined:
            print(f"[WARN] symbol(s) quarantined: {', '.join(quarantined)}")
        primary = str(cfg["trading"].get("symbol") or syms[0])
        if primary in quarantined:
            print(f"[FAIL] primary symbol {primary} has no usable quote")
            return 1
        print("[OK] symbol data feed")
    except Exception as exc:
        print(f"[3/4] account/symbol check FAILED: {exc}")
    finally:
        client.shutdown()

    # 4. LLM key
    llm_key_check(cfg)
    print("=" * 60)
    return 0


def llm_key_check(cfg: dict) -> None:
    from agents.llm_agent import load_api_key
    if not cfg["llm"].get("enabled", True):
        print("[4/4] LLM committee .................... disabled (quant-only mode)")
        return
    key = load_api_key(cfg["llm"])
    if key:
        model = cfg["llm"].get("gemini_model", "gemini-2.0-flash")
        print(f"[4/4] LLM committee .................... key found (model={model[:20]}...)")
    else:
        print("[4/4] LLM committee .................... NO KEY -> quant-only mode")
        print("      Put a Gemini key in config.json llm.api_key or keep the")
        print(f"      default file: {cfg['llm'].get('api_key_file')}")


def cmd_run(cfg: dict, dry_run: bool) -> int:
    from runner import TradingAgentRunner
    log = logging.getLogger()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S", force=True,
    )
    store = StateStore(cfg["logging"]["db_file"], cfg["logging"]["log_file"])
    store.attach_file_log(log)
    runner = TradingAgentRunner(cfg, store, dry_run=dry_run)
    try:
        runner.start()
        if dry_run:
            log.info("DRY-RUN: no real orders will be sent.")
        runner.run_forever()
    except Exception as exc:
        log.exception("Fatal: %s", exc)
        return 1
    finally:
        runner.stop()
        store.close()
    return 0


def cmd_backtest(cfg: dict, args) -> int:
    import backtest as bt
    if args.csv:
        df = bt.load_csv(args.csv)
        data_note = f"csv={args.csv} ({len(df)} bars)"
    else:
        df = bt.fetch_bars_mt5(cfg, args.symbol or cfg["trading"]["symbol"],
                               int(args.bars))
        data_note = (f"MT5 {args.symbol or cfg['trading']['symbol']} "
                     f"{cfg['trading']['timeframe']} ({len(df)} bars)")
    res = bt.run(df, cfg)
    bt.print_result(res, data_note)
    return 0


def _read_strategy_frame(cfg: dict, args):
    """Load OHLCV bars for research commands (CSV or MT5)."""
    import pandas as pd

    from market.data_engine import epoch_seconds, normalize_epoch

    if args.csv:
        df = pd.read_csv(args.csv)
        if "time" not in df.columns:
            if df.index.name in ("time", "dt"):
                df = df.reset_index()
            else:
                raise ValueError(
                    "CSV requires a 'time' column (epoch seconds or datetime).")
        if not pd.api.types.is_numeric_dtype(df["time"]):
            df["time"] = epoch_seconds(df["time"])
        else:
            df["time"] = normalize_epoch(df["time"])
        return df, f"csv={args.csv} ({len(df)} bars)"

    from agents.quant import to_frame
    from core.mt5_client import MT5Client

    symbol = args.symbol or cfg["trading"]["symbol"]
    client = MT5Client(cfg["mt5"])
    client.connect()
    try:
        resolved = client.resolve_symbol(symbol)
        raw = client.copy_bars(resolved, cfg["trading"]["timeframe"], int(args.bars))
        df = to_frame(raw).reset_index()
        df["time"] = epoch_seconds(df["time"])
    finally:
        client.shutdown()
    return df, f"MT5 {resolved} {cfg['trading']['timeframe']} ({len(df)} bars)"


def _strategy_overrides(args, df) -> tuple[dict, str]:
    """Research replay overrides plus a note about inferred price scale."""
    import pandas as pd

    overrides = {}
    if args.csv:
        overrides["symbol"] = args.symbol or "DEMO"
    for key, value in (
        ("point", args.point),
        ("digits", args.digits),
        ("spread_points", args.spread_points),
        ("slippage_points", args.slippage_points),
        ("warmup_bars", args.warmup),
    ):
        if value is not None:
            overrides[key] = value

    # A research CSV may not be the configured instrument.  Infer a sane
    # scale for FX-like prices instead of charging gold-sized spreads on EURUSD.
    note = ""
    median_close = float(pd.to_numeric(df["close"], errors="coerce").median())
    if median_close < 10.0:
        if "point" not in overrides:
            overrides["point"] = 0.00001
        if "digits" not in overrides:
            overrides["digits"] = 5
        if "spread_points" not in overrides:
            overrides["spread_points"] = 1.5
        if "slippage_points" not in overrides:
            overrides["slippage_points"] = 0.2
        note = " | FX-like scale inferred"
    return overrides, note


def cmd_strategies(cfg: dict, args) -> int:
    """Run the five-engine research replay and print per-strategy metrics."""
    from strategies.registry import build_strategies
    from strategies.replay import ReplayConfig, format_report, run_replay

    try:
        df, note = _read_strategy_frame(cfg, args)
    except (ValueError, OSError) as exc:
        print(f"strategies data error: {exc}")
        return 2
    overrides, scale_note = _strategy_overrides(args, df)
    note += scale_note

    replay_cfg = ReplayConfig.from_config(cfg, **overrides)
    engines = build_strategies(cfg)
    if args.only:
        engines = {args.only: engines[args.only]}
    result = run_replay(df, cfg, replay=replay_cfg, strategies=engines,
                        data_note=note)
    print(format_report(result))
    return 0


def cmd_walkforward(cfg: dict, args) -> int:
    """Run expanding-window out-of-sample folds plus one untouched holdout."""
    import json
    from pathlib import Path

    from research.walkforward import (
        WalkForwardConfig,
        format_report as format_walkforward,
        run_walk_forward,
    )
    from strategies.registry import build_strategies
    from strategies.replay import ReplayConfig

    try:
        df, note = _read_strategy_frame(cfg, args)
    except (ValueError, OSError) as exc:
        print(f"walkforward data error: {exc}")
        return 2
    overrides, scale_note = _strategy_overrides(args, df)
    note += scale_note
    replay_cfg = ReplayConfig.from_config(cfg, **overrides)

    def engines_factory(run_config):
        engines = build_strategies(run_config)
        if args.only:
            engines = {args.only: engines[args.only]}
        return engines

    wf = WalkForwardConfig(
        folds=int(args.folds),
        holdout_fraction=float(args.holdout),
        warmup_bars=int(replay_cfg.warmup_bars),
        min_test_bars=int(args.min_test_bars),
        mc_runs=int(args.mc_runs),
        mc_seed=int(args.seed),
        run_sensitivity=not bool(args.no_sensitivity),
    )
    try:
        result = run_walk_forward(
            df, cfg, replay=replay_cfg, wf=wf,
            engines_factory=engines_factory, data_note=note,
        )
    except ValueError as exc:
        print(f"walkforward error: {exc}")
        return 2

    print(format_walkforward(result))
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(result.as_dict(), handle, indent=2, allow_nan=False)
        print(f"JSON written to {path}")
    return 0 if result.passed else 1


def cmd_testorder(cfg: dict, args) -> int:
    """Fire ONE tiny market order then close it - proves the live trade path."""
    import logging as _lg
    from core.mt5_client import MT5Client, MT5Error
    from execution.executor import Executor
    from core.models import TradePlan, Consensus, LlmReview

    _lg.basicConfig(level=_lg.INFO, format="%(levelname)s %(name)s: %(message)s",
                    force=True)
    cfg["trading"]["symbol"] = args.symbol or cfg["trading"]["symbol"] \
        or (cfg["trading"].get("symbols") or ["EURUSD"])[0]
    client = MT5Client(cfg["mt5"])
    try:
        client.connect()
        symbol = client.resolve_symbol(cfg["trading"]["symbol"])
        client.prepare(symbol)
        info = MT5Client.symbol_settings(client.prepare(symbol))
        info_d = dict(info)
        volume = float(args.volume or info["volume_min"])
        tick = client.tick(symbol)
        point = info["point"]
        digits = info["digits"]
        spread = (tick.ask - tick.bid)
        dist = max(20 * point, client.prepare(symbol).trade_stops_level
                   + 5 * point, spread * 3)
        # decide direction by a quick EMA test so the order is legit-signed
        import pandas as pd
        bars = client.copy_bars(symbol, "M5", 120)
        close = pd.Series([b.close for b in bars])
        fast = close.ewm(span=12, adjust=False).mean().iloc[-1]
        slow = close.ewm(span=34, adjust=False).mean().iloc[-1]
        direction = 1 if fast > slow else -1
        if direction > 0:
            entry, sl, tp = tick.ask, tick.ask - dist, tick.ask + dist * 2
        else:
            entry, sl, tp = tick.bid, tick.bid + dist, tick.bid - dist * 2
        plan = TradePlan(
            symbol=symbol,
            action="buy" if direction > 0 else "sell",
            direction=direction,
            volume=volume,
            sl=round(sl, digits), tp=round(tp, digits), atr=dist,
            entry_hint=round(entry, digits),
            consensus=Consensus(direction=direction, strength=0.99, confidence=1.0),
            review=LlmReview(approved=True, action="approve", used_llm=False),
            reason="TESTORDER (opens + closes immediately)",
        )
        ex = Executor(client, magic=int(cfg["trading"]["magic"]),
                      risk_cfg=cfg["risk"], comment="TA-testorder")
        print(f"placing TEST order: {plan.summary()}")
        ok = ex.place(plan)
        if not ok:
            print("TEST ORDER FAILED - check retcode above (common: market closed)")
            return 1
        import time as _t
        _t.sleep(2)
        closed = ex.close_all(symbol)
        print(f"TEST COMPLETE: order placed and closed ({closed} position(s) closed).")
        print("Your bot can now take LIVE trades end-to-end.")
        return 0
    except MT5Error as e:
        print("testorder failed:", e)
        return 1
    finally:
        client.shutdown()


def _ea_settings(cfg: dict) -> dict:
    ea = cfg.get("ea") or {}
    return {
        "host": ea.get("host") or "127.0.0.1",
        "port": int(ea.get("port") or 8765),
        "token_file": ea.get("token_file") or "state/ea_token.txt",
        "heartbeat_ms": int(ea.get("heartbeat_ms") or 1000),
        "ack_timeout_seconds": float(ea.get("ack_timeout_seconds") or 10.0),
        "disconnect_grace_seconds": float(
            ea.get("disconnect_grace_seconds") or 30.0),
    }


def _restrict_token_permissions(path: str) -> None:
    """Best-effort owner-only permissions for the shared token file."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    if not user:
        return
    principal = f"{domain}\\{user}" if domain else user
    try:
        import subprocess
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{principal}:F"],
            check=False, capture_output=True, text=True, timeout=10)
        # Never leave the token unreadable by the account that just created it.
        with open(path, encoding="utf-8") as handle:
            handle.read()
    except (OSError, subprocess.SubprocessError):
        try:
            import subprocess
            subprocess.run(["icacls", path, "/reset"],
                           check=False, capture_output=True, text=True, timeout=10)
            subprocess.run(
                ["icacls", path, "/grant:r", f"{principal}:F"],
                check=False, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


def _mt5_common_files() -> str:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return ""
    return os.path.join(appdata, "MetaQuotes", "Terminal", "Common", "Files")


def cmd_ea_token(cfg: dict, args) -> int:
    """Create the shared Python/EA token and copy it to MT5 common files."""
    from execution.ea_protocol import TOKEN_ENV, create_token

    settings = _ea_settings(cfg)
    token_file = args.token_file or settings["token_file"]
    value, path = create_token(token_file, force=bool(args.force))
    _restrict_token_permissions(path)
    print(f"EA token: {os.path.abspath(path)}")

    common = args.common_dir or _mt5_common_files()
    if common and not args.no_mt5:
        folder = os.path.join(common, "TradingAgents")
        try:
            os.makedirs(folder, exist_ok=True)
            common_path = os.path.join(folder, "token.txt")
            with open(common_path, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
            _restrict_token_permissions(common_path)
            print(f"EA token copied to: {common_path}")
        except OSError as exc:
            print(f"WARNING: could not copy token to MT5 common files: {exc}",
                  file=sys.stderr)

    if os.environ.get(TOKEN_ENV, "").strip():
        print(f"WARNING: {TOKEN_ENV} is set and takes precedence over the file",
              file=sys.stderr)
        if os.environ[TOKEN_ENV].strip() != value:
            print("WARNING: the environment token differs from the file token",
                  file=sys.stderr)
    print("Set the EA input InpTokenFile to TradingAgents\\token.txt "
          "(relative to the MQL5 common files folder).")
    return 0


def cmd_ea_server(cfg: dict, args) -> int:
    """Run the loopback bridge that a native EA connects to."""
    import time
    from execution.ea_client import EAOrderBridge, EAError
    from execution.ea_protocol import (
        AuthenticationError, DEFAULT_HOST, DEFAULT_PORT, load_token)

    settings = _ea_settings(cfg)
    host = args.host or settings["host"]
    port = args.port or settings["port"]
    token_file = args.token_file or settings["token_file"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("EA bridge refuses non-loopback hosts without TLS", file=sys.stderr)
        return 2
    try:
        token = load_token(token_file)
    except AuthenticationError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    bridge = EAOrderBridge(
        token,
        host=host if host != "localhost" else DEFAULT_HOST,
        port=port or DEFAULT_PORT,
        ack_timeout=settings["ack_timeout_seconds"],
    )
    try:
        bridge.start()
    except OSError as exc:
        print(f"EA bridge could not bind {host}:{port}: {exc}", file=sys.stderr)
        return 2

    print(f"EA bridge listening on {host}:{bridge.bound_port} "
          f"(token file {os.path.abspath(token_file)})")
    print("Attach TradingAgentsEA in MetaTrader 5; it is the TCP client.")
    if not (cfg.get("ea") or {}).get("enabled", False):
        print("NOTE: ea.enabled is false; this bridge is for supervised testing only.")
    deadline = time.time() + float(args.seconds) if args.seconds else None
    try:
        while deadline is None or time.time() < deadline:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping EA bridge (Ctrl+C).")
    finally:
        status = bridge.status()
        bridge.stop()
    print(f"EA bridge stopped: connected={status['connected']} "
          f"authenticated={status['authenticated']} "
          f"positions={status['positions']} pending={status['pending']}")
    return 0


def cmd_status(cfg: dict) -> int:
    from core.mt5_client import MT5Client
    store = StateStore(cfg["logging"]["db_file"])
    try:
        client = MT5Client(cfg["mt5"])
        client.connect()
        acc = client.account()
        print(f"account {acc.login} ({acc.server if hasattr(acc, 'server') else ''})"
              f"  balance={acc.balance:.2f}  equity={acc.equity:.2f}  "
              f"floating={acc.profit:+.2f}  margin_level={acc.margin_level:.0f}")
        magic = int(cfg["trading"]["magic"])
        print(f"bot daily P/L ................ {client.daily_bot_profit(magic):+.2f} "
              f"{acc.currency}")
        poss = client.positions(magic=magic)
        if poss:
            print("open bot positions:")
            for p in poss:
                d = "BUY" if p.type == 0 else "SELL"
                print(f"  #{p.ticket} {d} {p.symbol} {p.volume} @ {p.price_open:.5f} "
                      f"sl={p.sl} tp={p.tp} pl={p.profit:+.2f}")
        else:
            print("open bot positions: none")
        client.shutdown()
    except Exception as exc:
        print(f"MT5 unavailable: {exc}")
    st = store.stats()
    print(f"journal: {st['decisions']} decisions, {st['executed']} executed, "
          f"{st['filled_orders']} filled orders")
    for row in reversed(store.recent_decisions(10)):
        ts, action, sym, vol = row["ts"], row["action"], row["symbol"], row["volume"]
        import time as _t
        print(f"  {_t.strftime('%m-%d %H:%M', _t.localtime(ts))} {action:12s} {sym} "
              f"vol={vol:.2f} llm={row['llm_action']} :: {row['reason'][:70]}")
    store.close()
    return 0


def cmd_dashboard(cfg: dict, args) -> int:
    """Serve the local FastAPI + React control room."""
    import uvicorn

    from api.app import create_app
    from api.services import ApiSettings

    if args.host and args.host not in ("127.0.0.1", "localhost", "::1"):
        print("dashboard refuses non-loopback hosts; use an SSH tunnel instead",
              file=sys.stderr)
        return 2
    api_settings = ApiSettings.load(root=ROOT, config_path=args.config)
    app = create_app(settings=api_settings)
    host = "127.0.0.1" if args.host in (None, "localhost") else args.host
    port = int(args.port or 8766)
    print(f"Dashboard: http://{host}:{port}")
    print(f"API docs:  http://{host}:{port}/api/docs")
    print(f"Token:     {api_settings.token_file} (never printed here)")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TradingAgents for MT5")
    parser.add_argument("command", choices=["check", "run", "backtest", "status",
                                            "testorder", "strategies",
                                            "walkforward",
                                            "ea-token", "ea-server", "dashboard"])
    parser.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--bars", default=5000, type=int)
    parser.add_argument("--csv", default=None)
    parser.add_argument("--volume", default=None, type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", default=None,
                        choices=["trend", "breakout", "smc", "momentum",
                                 "mean_reversion"],
                        help="run a single strategy engine (default: all five)")
    parser.add_argument("--point", default=None, type=float)
    parser.add_argument("--digits", default=None, type=int)
    parser.add_argument("--spread-points", default=None, type=float)
    parser.add_argument("--slippage-points", default=None, type=float)
    parser.add_argument("--warmup", default=None, type=int)

    # walk-forward validation options
    parser.add_argument("--folds", default=4, type=int,
                        help="walkforward: number of expanding test folds")
    parser.add_argument("--holdout", default=0.25, type=float,
                        help="walkforward: holdout fraction of bars (0-0.5)")
    parser.add_argument("--min-test-bars", default=200, type=int,
                        help="walkforward: minimum bars per test fold")
    parser.add_argument("--mc-runs", default=2000, type=int,
                        help="walkforward: bootstrap resamples")
    parser.add_argument("--seed", default=42, type=int,
                        help="walkforward: Monte Carlo seed")
    parser.add_argument("--json", default=None,
                        help="walkforward: write the full result as JSON")
    parser.add_argument("--no-sensitivity", action="store_true",
                        help="walkforward: skip the parameter-sensitivity grid")

    # EA bridge / token options
    parser.add_argument("--token-file", default=None,
                        help="shared EA token file (default: state/ea_token.txt)")
    parser.add_argument("--force", action="store_true",
                        help="ea-token: replace an existing token")
    parser.add_argument("--no-mt5", action="store_true",
                        help="ea-token: do not copy into the MT5 common folder")
    parser.add_argument("--common-dir", default=None,
                        help="ea-token: override the MT5 Common\\Files folder")
    parser.add_argument("--host", default=None,
                        help="ea-server: bind host (loopback only)")
    parser.add_argument("--port", default=None, type=int,
                        help="ea-server: bind port (default 8765)")
    parser.add_argument("--seconds", default=0.0, type=float,
                        help="ea-server: stop after N seconds (0 = run until Ctrl+C)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    if args.symbol:
        cfg["trading"]["symbol"] = args.symbol

    os.chdir(ROOT)
    if args.command == "check":
        return cmd_check(cfg)
    if args.command == "run":
        return cmd_run(cfg, args.dry_run)
    if args.command == "backtest":
        return cmd_backtest(cfg, args)
    if args.command == "strategies":
        return cmd_strategies(cfg, args)
    if args.command == "walkforward":
        return cmd_walkforward(cfg, args)
    if args.command == "testorder":
        return cmd_testorder(cfg, args)
    if args.command == "ea-token":
        return cmd_ea_token(cfg, args)
    if args.command == "ea-server":
        return cmd_ea_server(cfg, args)
    if args.command == "dashboard":
        return cmd_dashboard(cfg, args)
    return cmd_status(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
