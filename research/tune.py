"""Cost-aware tuning with a chronological train/validation/holdout split.

The strategy itself is fixed; this tool only searches agent weights, the
consensus threshold, the dead-market ratio and a few exit variants.  It is
deliberately conservative about what it claims:

  * the three windows are contiguous and never overlap;
  * every replay pays commission, slippage and the configured spread floor;
  * train only narrows the field (top-K by train net PnL);
  * the winner is chosen on **validation** net PnL among candidates that
    clear a frequency floor — the holdout is never used for selection;
  * the holdout is replayed exactly once for the winner and once for the live
    baseline (or reused when they are the same configuration);
  * ``--apply`` refuses to write unless the holdout has positive expectancy,
    PF >= the gate, enough frequency and better PnL, expectancy and PF than
    the baseline.

Per-bar votes come from the signal cache (``backtest.py --dump-signals``), so
this study reweights the five quant agents and changes thresholds/exit rules;
it does not retune indicator parameters and does not include Kronos or Gemini.
No LLM veto, news filter, guardian, max-spread gate, trading hours or
shared-account concurrency is simulated.  This is research evidence, not a
promotion mechanism.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import sys
import time
from copy import deepcopy
from dataclasses import asdict, dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_schema import validate_config                 # noqa: E402
from research import backtest as bt                            # noqa: E402
from research.measure_upgrade import (                         # noqa: E402
    DEFAULT_COSTS, floor_for, row_from, summarize,
)

ROOT = bt.ROOT
STATE = bt.STATE

AGENT_NAMES = ("trend", "momentum", "reversion", "breakout", "pullback")

# Live weights are read from config.json; these are the challenger presets.
WEIGHT_PRESETS = {
    "trend_hi": {"trend": 1.4, "momentum": 0.8, "reversion": 0.3,
                 "breakout": 0.8, "pullback": 0.5},
    "momentum_hi": {"trend": 0.9, "momentum": 1.3, "reversion": 0.3,
                    "breakout": 1.0, "pullback": 0.6},
    "reversion_hi": {"trend": 0.9, "momentum": 0.8, "reversion": 0.8,
                     "breakout": 0.8, "pullback": 0.9},
    "balanced": {"trend": 1.0, "momentum": 1.0, "reversion": 0.5,
                 "breakout": 1.0, "pullback": 0.8},
}
THRESHOLDS = (0.20, 0.30, 0.40)
DEAD_MARKETS = (0.62, 0.75)

# Exit variants only touch settings the replay and the live executor share.
EXIT_VARIANTS = {
    "live": {},
    "no_partial": {"risk": {"partial_close_enabled": False}},
    "wider_tp": {"agents": {"atr_tp_mult": 4.0}},
    "no_trail": {"risk": {"trailing_atr": 0.0, "trailing_step_atr": 0.0}},
}

MIN_TRADES_PER_1000_BARS = 120.0
MIN_HOLDOUT_PF = 1.05


# --------------------------------------------------------------------- split
@dataclass(frozen=True)
class StudySplit:
    """Contiguous windows over bar indices: [start, end)."""

    train_start: int
    train_end: int
    validation_start: int
    validation_end: int
    holdout_start: int
    holdout_end: int

    def window(self, name: str) -> tuple[int, int]:
        if name == "train":
            return self.train_start, self.train_end
        if name == "validation":
            return self.validation_start, self.validation_end
        if name == "holdout":
            return self.holdout_start, self.holdout_end
        raise ValueError(f"unknown window {name!r}")


def build_study_split(
    n_bars: int,
    *,
    warmup_bars: int = bt.DEFAULT_BARS_HISTORY,
    train_fraction: float = 0.50,
    validation_fraction: float = 0.25,
    holdout_fraction: float = 0.25,
    tail_bars: int = 6,
    min_window_bars: int = 100,
) -> StudySplit:
    """Split ``n_bars`` into train, validation and holdout, in that order.

    Fractions apply to the evaluable region after ``warmup_bars`` and before
    the trailing ``tail_bars``.  The three windows tile that region exactly,
    so they cannot overlap and no bar belongs to two windows.
    """
    n_bars = int(n_bars)
    warmup_bars = int(warmup_bars)
    tail_bars = max(0, int(tail_bars))
    min_window_bars = max(1, int(min_window_bars))
    if warmup_bars < 0:
        raise ValueError("warmup_bars must be non-negative")
    fractions = (float(train_fraction), float(validation_fraction),
                 float(holdout_fraction))
    if any(value <= 0.0 for value in fractions):
        raise ValueError("train/validation/holdout fractions must be positive")
    if abs(sum(fractions) - 1.0) > 1e-9:
        raise ValueError("train/validation/holdout fractions must sum to 1")

    usable_end = n_bars - tail_bars
    evaluable = usable_end - warmup_bars
    if evaluable < 3 * min_window_bars:
        raise ValueError(
            f"not enough bars: {evaluable} evaluable bars for three windows "
            f"of at least {min_window_bars}")

    holdout_len = int(round(evaluable * fractions[2]))
    validation_len = int(round(evaluable * fractions[1]))
    train_len = evaluable - validation_len - holdout_len
    if min(train_len, validation_len, holdout_len) < min_window_bars:
        raise ValueError(
            f"window too small: train={train_len}, validation={validation_len}, "
            f"holdout={holdout_len}, minimum={min_window_bars}")

    train_start = warmup_bars
    train_end = train_start + train_len
    validation_start = train_end
    validation_end = validation_start + validation_len
    holdout_start = validation_end
    holdout_end = holdout_start + holdout_len
    if holdout_end != usable_end:
        raise AssertionError("split boundaries do not tile the sample")
    return StudySplit(train_start, train_end, validation_start,
                      validation_end, holdout_start, holdout_end)


# ----------------------------------------------------------------- candidate
@dataclass(frozen=True)
class Candidate:
    preset: str
    weights: tuple[tuple[str, float], ...]
    threshold: float
    dead_market: float
    exit_variant: str = "live"

    @property
    def weight_map(self) -> dict[str, float]:
        return dict(self.weights)

    @property
    def label(self) -> str:
        return (f"{self.preset}:thr{self.threshold:.2f}:"
                f"dead{self.dead_market:.2f}:{self.exit_variant}")

    def apply(self, cfg: dict) -> dict:
        """Return a deep copy of ``cfg`` with this candidate applied."""
        out = deepcopy(cfg)
        agents = out.setdefault("agents", {})
        risk = out.setdefault("risk", {})
        for name, value in self.weights:
            agents[f"weight_{name}"] = float(value)
        agents["consensus_threshold"] = float(self.threshold)
        risk["dead_market_atr_ratio"] = float(self.dead_market)
        for section, values in EXIT_VARIANTS[self.exit_variant].items():
            out.setdefault(section, {}).update(deepcopy(values))
        return out

    def config_patch(self, cfg: dict) -> dict:
        """Only the keys that differ from the live config."""
        patch: dict[str, dict] = {}
        agents = cfg.get("agents") or {}
        risk = cfg.get("risk") or {}
        for name, value in self.weights:
            key = f"weight_{name}"
            if not _same(agents.get(key), value):
                patch.setdefault("agents", {})[key] = float(value)
        if not _same(agents.get("consensus_threshold"), self.threshold):
            patch.setdefault("agents", {})["consensus_threshold"] = float(
                self.threshold)
        if not _same(risk.get("dead_market_atr_ratio"), self.dead_market):
            patch.setdefault("risk", {})["dead_market_atr_ratio"] = float(
                self.dead_market)
        for section, values in EXIT_VARIANTS[self.exit_variant].items():
            for key, value in values.items():
                if not _same((cfg.get(section) or {}).get(key), value):
                    patch.setdefault(section, {})[key] = value
        return patch


def _same(left, right) -> bool:
    try:
        return abs(float(left) - float(right)) < 1e-12
    except (TypeError, ValueError):
        return left == right


def _signature(candidate: Candidate) -> tuple:
    return (candidate.weights, float(candidate.threshold),
            float(candidate.dead_market), candidate.exit_variant)


def live_weights(cfg: dict) -> tuple[tuple[str, float], ...]:
    agents = cfg.get("agents") or {}
    return tuple((name, float(agents.get(f"weight_{name}", 1.0)))
                 for name in AGENT_NAMES)


def baseline_candidate(cfg: dict) -> Candidate:
    """The live configuration, used as the holdout comparison."""
    agents = cfg.get("agents") or {}
    risk = cfg.get("risk") or {}
    return Candidate(
        "baseline", live_weights(cfg),
        float(agents.get("consensus_threshold", 0.3)),
        float(risk.get("dead_market_atr_ratio", 0.62)),
        "live",
    )


def build_candidates(
    cfg: dict,
    *,
    thresholds=THRESHOLDS,
    dead_markets=DEAD_MARKETS,
    exit_variants=tuple(EXIT_VARIANTS),
) -> tuple[list[Candidate], Candidate]:
    """Build the fixed search grid, excluding a duplicate of the baseline."""
    baseline = baseline_candidate(cfg)
    baseline_sig = _signature(baseline)
    presets = {"live": dict(live_weights(cfg)), **WEIGHT_PRESETS}
    seen = {baseline_sig}
    candidates: list[Candidate] = []
    for name, weights in presets.items():
        wtuple = tuple((agent, float(weights.get(agent, 1.0)))
                       for agent in AGENT_NAMES)
        for threshold, dead, variant in itertools.product(
                thresholds, dead_markets, exit_variants):
            candidate = Candidate(name, wtuple, float(threshold),
                                  float(dead), variant)
            signature = _signature(candidate)
            if signature in seen:
                continue
            seen.add(signature)
            candidates.append(candidate)
    return candidates, baseline


# -------------------------------------------------------------------- costs
def resolve_costs(
    cfg: dict,
    *,
    commission: float | None = None,
    slippage_points: float | None = None,
    spread_points: float | None = None,
    start_equity: float | None = None,
) -> dict:
    """Merge labelled cost defaults, the config block and CLI overrides."""
    settings = {**DEFAULT_COSTS, **(cfg.get("backtest") or {})}
    if commission is not None:
        settings["commission_per_lot_round_turn"] = float(commission)
    if slippage_points is not None:
        settings["slippage_points"] = float(slippage_points)
    if spread_points is not None:
        settings["spread_points"] = float(spread_points)
    if start_equity is not None:
        settings["start_equity"] = float(start_equity)
    settings.setdefault("start_equity", bt.DEFAULT_EQUITY)
    settings["spread_points_by_symbol"] = {
        str(symbol).upper(): float(value)
        for symbol, value in (settings.get("spread_points_by_symbol") or {}).items()
    }
    return settings


# ---------------------------------------------------------------- frequency
def trades_per_1000_bars(trades: int, bars: int) -> float:
    bars = int(bars)
    return float(trades) / bars * 1000.0 if bars > 0 else 0.0


def passes_frequency_floor(trades: int, bars: int, floor: float) -> bool:
    return int(bars) > 0 and trades_per_1000_bars(trades, bars) + 1e-12 >= float(floor)


# ------------------------------------------------------------------ evaluate
def evaluate_window(
    caches: dict,
    cfg: dict,
    candidate: Candidate,
    window: tuple[int, int],
    *,
    symbols: list[str],
    timeframe: str,
    costs: dict,
) -> dict:
    """Replay one candidate over one window for every symbol."""
    rows: dict[str, dict] = {}
    total_bars = 0
    variant_cfg = candidate.apply(cfg)
    for symbol in symbols:
        cache = caches[symbol]
        start = max(0, int(window[0]))
        end = min(len(cache["df"]), int(window[1]))
        span = max(0, end - start)
        if span <= 0:
            rows[symbol] = {
                "trades": 0, "pnl": 0.0, "win_rate_pct": 0.0,
                "profit_factor": 0.0, "max_drawdown": 0.0, "avg_r": None,
                "avg_pnl_per_trade": 0.0, "wins": 0, "gross_win": 0.0,
                "gross_loss": 0.0, "exits": {}, "bars": 0,
            }
            continue
        result = bt.replay(
            cache["df"], bt.symbol_settings(symbol), variant_cfg,
            cache["votes"], cache["confs"], list(cache["agents"]),
            candidate.weight_map, candidate.threshold,
            dead_market_ratio=candidate.dead_market,
            start=start, end=end, symbol=symbol, timeframe=timeframe,
            commission_per_lot=float(
                costs.get("commission_per_lot_round_turn") or 0.0),
            slippage_points=float(costs.get("slippage_points") or 0.0),
            spread_floor_points=floor_for(costs, symbol),
            start_equity=float(costs.get("start_equity") or bt.DEFAULT_EQUITY),
        )
        metrics = bt.metrics(result["trades"], result["curve"],
                             timeframe=timeframe,
                             start_equity=result.get("start_equity"))
        row = row_from(metrics, result["trades"])
        row["bars"] = span
        rows[symbol] = row
        total_bars += span
    return {"metrics": summarize(rows), "symbols": rows, "bars": total_bars}


def _expectancy(metrics: dict) -> float:
    """Per-trade USD expectancy from either aggregate row shape."""
    value = metrics.get("avg_pnl_per_trade")
    if value is None:
        value = metrics.get("expectancy_usd")
    if value is None:
        trades = int(metrics.get("trades") or 0)
        value = (float(metrics.get("pnl") or 0.0) / trades) if trades else 0.0
    return float(value or 0.0)


def selection_key(row: dict) -> tuple[float, float, float]:
    metrics = row.get("metrics") or {}
    return (float(metrics.get("pnl") or 0.0),
            float(metrics.get("profit_factor") or 0.0),
            _expectancy(metrics))


def select_winner(rows: list[dict], *, min_trades_per_1000_bars: float):
    """Highest validation net PnL among candidates clearing the floor.

    Deliberately reads only ``metrics``/``bars``.  No holdout field exists on
    these rows, so holdout data cannot influence the choice.
    """
    eligible = [
        row for row in rows
        if passes_frequency_floor(
            (row.get("metrics") or {}).get("trades", 0),
            row.get("bars", 0), min_trades_per_1000_bars)
    ]
    return max(eligible, key=selection_key) if eligible else None


# -------------------------------------------------------------------- gates
@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str


def holdout_gates(
    winner: dict,
    baseline: dict | None,
    *,
    min_trades_per_1000_bars: float,
    min_profit_factor: float = MIN_HOLDOUT_PF,
    min_expectancy_usd: float = 0.0,
    primary_symbol: str | None = None,
) -> list[Gate]:
    metrics = winner.get("metrics") or {}
    trades = int(metrics.get("trades") or 0)
    bars = int(winner.get("bars") or 0)
    expectancy = _expectancy(metrics)
    profit_factor = float(metrics.get("profit_factor") or 0.0)
    pnl = float(metrics.get("pnl") or 0.0)
    gates = [
        Gate("holdout_frequency",
             passes_frequency_floor(trades, bars, min_trades_per_1000_bars),
             f"{trades_per_1000_bars(trades, bars):.1f} trades/1k bars "
             f">= {float(min_trades_per_1000_bars):.1f}"),
        Gate("holdout_expectancy",
             expectancy > float(min_expectancy_usd),
             f"${expectancy:+.2f}/trade > ${float(min_expectancy_usd):.2f}"),
        Gate("holdout_profit_factor",
             profit_factor >= float(min_profit_factor),
             f"PF {profit_factor:.2f} >= {float(min_profit_factor):.2f}"),
        Gate("holdout_pnl_positive", pnl > 0.0, f"${pnl:+.2f} > $0"),
    ]
    if baseline is None:
        gates.append(Gate("holdout_beats_baseline", False,
                          "baseline was not evaluated"))
    else:
        base_metrics = baseline.get("metrics") or {}
        base_pnl = float(base_metrics.get("pnl") or 0.0)
        base_expectancy = _expectancy(base_metrics)
        base_pf = float(base_metrics.get("profit_factor") or 0.0)
        improved = (pnl > base_pnl and expectancy > base_expectancy
                    and profit_factor >= base_pf)
        gates.append(Gate(
            "holdout_beats_baseline", improved,
            f"${pnl:+.2f} vs ${base_pnl:+.2f}, "
            f"${expectancy:+.2f}/trade vs ${base_expectancy:+.2f}, "
            f"PF {profit_factor:.2f} vs {base_pf:.2f}"))
    if primary_symbol:
        key = str(primary_symbol).upper()
        winner_symbol = (winner.get("symbols") or {}).get(key)
        baseline_symbol = ((baseline or {}).get("symbols") or {}).get(key)
        if winner_symbol is None or baseline_symbol is None:
            gates.append(Gate(
                "holdout_primary_symbol", False,
                f"{key} is missing from the holdout rows"))
        else:
            symbol_pnl = float(winner_symbol.get("pnl") or 0.0)
            base_symbol_pnl = float(baseline_symbol.get("pnl") or 0.0)
            symbol_expectancy = _expectancy(winner_symbol)
            base_symbol_expectancy = _expectancy(baseline_symbol)
            passed = (symbol_pnl > 0.0 and symbol_pnl > base_symbol_pnl
                      and symbol_expectancy > base_symbol_expectancy)
            gates.append(Gate(
                "holdout_primary_symbol", passed,
                f"{key} ${symbol_pnl:+.2f} vs baseline "
                f"${base_symbol_pnl:+.2f}, "
                f"${symbol_expectancy:+.2f}/trade vs "
                f"${base_symbol_expectancy:+.2f}"))
    return gates


# --------------------------------------------------------------- study loop
def _row(candidate: Candidate, window_name: str, result: dict) -> dict:
    return {
        "label": candidate.label,
        "preset": candidate.preset,
        "threshold": float(candidate.threshold),
        "dead_market": float(candidate.dead_market),
        "exit_variant": candidate.exit_variant,
        "window": window_name,
        "metrics": result.get("metrics") or {},
        "bars": int(result.get("bars") or 0),
        "symbols": result.get("symbols") or {},
        "_candidate": candidate,
    }


def public_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {key: value for key, value in row.items() if key != "_candidate"}


def run_study(
    caches: dict,
    cfg: dict,
    split: StudySplit,
    *,
    candidates: list[Candidate],
    symbols: list[str],
    timeframe: str,
    costs: dict,
    top_k: int = 12,
    min_trades_per_1000_bars: float = MIN_TRADES_PER_1000_BARS,
    min_holdout_pf: float = MIN_HOLDOUT_PF,
    min_holdout_expectancy: float = 0.0,
    primary_symbol: str | None = None,
    evaluate_fn=evaluate_window,
) -> dict:
    """Train screen -> validation selection -> one-shot holdout gates."""
    baseline = baseline_candidate(cfg)

    train_rows = []
    for candidate in candidates:
        result = evaluate_fn(
            caches, cfg, candidate, split.window("train"),
            symbols=symbols, timeframe=timeframe, costs=costs)
        train_rows.append(_row(candidate, "train", result))

    train_eligible = [
        row for row in train_rows
        if passes_frequency_floor(
            (row.get("metrics") or {}).get("trades", 0),
            row.get("bars", 0), min_trades_per_1000_bars)
    ]
    train_eligible.sort(key=selection_key, reverse=True)
    advancing = train_eligible[:max(1, int(top_k))]
    if not advancing:
        return {
            "split": split,
            "train": train_rows,
            "advancing": [],
            "validation": [],
            "holdout": {},
            "winner": None,
            "winner_candidate": None,
            "baseline": baseline,
            "gates": [],
            "approved": False,
            "reason": "no train candidate cleared the frequency floor",
        }

    validation_candidates = [row["_candidate"] for row in advancing]
    if all(candidate.label != baseline.label
           for candidate in validation_candidates):
        validation_candidates.append(baseline)

    validation_rows = []
    for candidate in validation_candidates:
        result = evaluate_fn(
            caches, cfg, candidate, split.window("validation"),
            symbols=symbols, timeframe=timeframe, costs=costs)
        validation_rows.append(_row(candidate, "validation", result))

    winner = select_winner(
        validation_rows, min_trades_per_1000_bars=min_trades_per_1000_bars)

    holdout: dict[str, dict] = {}
    gates: list[Gate] = []
    if winner is not None:
        winner_candidate = winner["_candidate"]
        holdout[winner_candidate.label] = _row(
            winner_candidate, "holdout",
            evaluate_fn(caches, cfg, winner_candidate, split.window("holdout"),
                        symbols=symbols, timeframe=timeframe, costs=costs))
        if winner_candidate.label == baseline.label:
            baseline_holdout = holdout[winner_candidate.label]
        else:
            baseline_holdout = _row(
                baseline, "holdout",
                evaluate_fn(caches, cfg, baseline, split.window("holdout"),
                            symbols=symbols, timeframe=timeframe, costs=costs))
            holdout[baseline.label] = baseline_holdout
        gates = holdout_gates(
            holdout[winner_candidate.label], baseline_holdout,
            min_trades_per_1000_bars=min_trades_per_1000_bars,
            min_profit_factor=min_holdout_pf,
            min_expectancy_usd=min_holdout_expectancy,
            primary_symbol=primary_symbol)

    return {
        "split": split,
        "train": train_rows,
        "advancing": advancing,
        "validation": validation_rows,
        "holdout": holdout,
        "winner": winner,
        "winner_candidate": winner["_candidate"] if winner else None,
        "baseline": baseline,
        "gates": gates,
        "approved": bool(winner) and all(gate.passed for gate in gates),
        "reason": "",
    }


# -------------------------------------------------------------------- apply
def merge_patch(cfg: dict, patch: dict) -> dict:
    out = deepcopy(cfg)
    for section, values in (patch or {}).items():
        if isinstance(values, dict):
            out.setdefault(section, {}).update(deepcopy(values))
        else:
            out[section] = deepcopy(values)
    return out


def apply_patch_file(path: str, patch: dict,
                     *, backup_suffix: str = ".bak_pre_tune") -> dict:
    """Validate the patched config first, then write it atomically."""
    with open(path, encoding="utf-8") as handle:
        cfg = json.load(handle)
    patched = merge_patch(cfg, patch)
    errors = validate_config(patched)
    if errors:
        raise SystemExit("refusing to apply an invalid config: "
                         + "; ".join(errors))
    shutil.copy2(path, path + backup_suffix)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(patched, handle, indent=2)
    os.replace(tmp, path)
    return patched


# ---------------------------------------------------------------------- CLI
def _print_rows(title: str, rows: list[dict], limit: int = 10) -> None:
    print(f"\n{title}")
    print(f"{'label':<52} {'trades':>6} {'pnl':>11} {'PF':>5} {'1k bars':>8}")
    for row in sorted(rows, key=selection_key, reverse=True)[:limit]:
        metrics = row.get("metrics") or {}
        rate = trades_per_1000_bars(metrics.get("trades", 0),
                                    row.get("bars", 0))
        print("%-52s %6d %+11.1f %5.2f %8.1f" % (
            row["label"], int(metrics.get("trades") or 0),
            float(metrics.get("pnl") or 0.0),
            float(metrics.get("profit_factor") or 0.0), rate))


def _split_dates(df, split: StudySplit) -> dict:
    times = df["time"].astype(str)
    out = {}
    for name in ("train", "validation", "holdout"):
        start, end = split.window(name)
        out[name] = {"start": str(times.iloc[start]),
                     "end": str(times.iloc[end - 1]),
                     "bars": int(end - start)}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cost-aware train/validation/holdout signal study")
    parser.add_argument("--days", type=int, default=120,
                        help="days used only when a signal cache is missing")
    parser.add_argument("--timeframe", type=str, default=None,
                        help="default: trading.timeframe from config")
    parser.add_argument("--symbols", type=str, default=None,
                        help="default: trading.symbols from config")
    parser.add_argument("--offline", action="store_true",
                        help="build a missing cache from the CSV, never MT5")
    parser.add_argument("--top-k", type=int, default=12,
                        help="train survivors evaluated on validation")
    parser.add_argument("--min-trades-per-1000-bars", type=float,
                        default=MIN_TRADES_PER_1000_BARS)
    parser.add_argument("--min-holdout-pf", type=float, default=MIN_HOLDOUT_PF)
    parser.add_argument("--min-holdout-expectancy", type=float, default=0.0)
    parser.add_argument("--commission", type=float, default=None,
                        help="round-turn commission per lot override")
    parser.add_argument("--slippage-points", type=float, default=None)
    parser.add_argument("--spread-points", type=float, default=None,
                        help="global spread floor override; per-symbol "
                             "floors in the cost block still win")
    parser.add_argument("--start-equity", type=float, default=None)
    parser.add_argument("--apply", action="store_true",
                        help="write the winner only if every holdout gate passes")
    parser.add_argument("--out", type=str,
                        default=os.path.join(STATE, "tuning_result.json"))
    args = parser.parse_args()

    config_path = os.path.join(ROOT, "config.json")
    with open(config_path, encoding="utf-8") as handle:
        cfg = json.load(handle)
    timeframe = str(args.timeframe or cfg["trading"].get("timeframe")
                    or "M15").upper()
    symbols = ([symbol.strip().upper()
                for symbol in args.symbols.split(",") if symbol.strip()]
               if args.symbols else
               [str(symbol).upper() for symbol in cfg["trading"]["symbols"]])
    primary_symbol = str(cfg["trading"].get("symbol") or "").upper() or None
    if primary_symbol not in symbols:
        primary_symbol = None

    caches: dict[str, dict] = {}
    for symbol in symbols:
        try:
            caches[symbol] = bt.load_signals(symbol, timeframe=timeframe)
        except SystemExit:
            print(f"building signal cache for {symbol} {timeframe} ...",
                  flush=True)
            bt.dump_signals([symbol], args.days, timeframe=timeframe,
                            offline=args.offline)
            caches[symbol] = bt.load_signals(symbol, timeframe=timeframe)

    n_bars = min(len(cache["df"]) for cache in caches.values())
    split = build_study_split(n_bars)
    costs = resolve_costs(
        cfg, commission=args.commission,
        slippage_points=args.slippage_points,
        spread_points=args.spread_points,
        start_equity=args.start_equity)
    candidates, baseline = build_candidates(cfg)

    print(f"bars: {n_bars} | timeframe {timeframe} | symbols "
          f"{','.join(symbols)}")
    for name, dates in _split_dates(
            caches[symbols[0]]["df"], split).items():
        print("  %-10s %s -> %s  (%d bars)" % (
            name, dates["start"], dates["end"], dates["bars"]))
    print("costs: commission $%.2f/lot round turn, slippage %.2f points, "
          "floors %s" % (
              float(costs["commission_per_lot_round_turn"]),
              float(costs["slippage_points"]),
              {symbol: floor_for(costs, symbol) for symbol in symbols}))
    print(f"candidates: {len(candidates)} challengers + live baseline | "
          f"frequency floor {args.min_trades_per_1000_bars:.1f}/1k bars")
    if primary_symbol:
        print(f"primary symbol gate: {primary_symbol}")

    result = run_study(
        caches, cfg, split, candidates=candidates, symbols=symbols,
        timeframe=timeframe, costs=costs, top_k=args.top_k,
        min_trades_per_1000_bars=args.min_trades_per_1000_bars,
        min_holdout_pf=args.min_holdout_pf,
        min_holdout_expectancy=args.min_holdout_expectancy,
        primary_symbol=primary_symbol)

    _print_rows("TRAIN SURVIVORS (highest train PnL that cleared the floor)",
                result["advancing"])
    _print_rows("VALIDATION (winner is chosen here)", result["validation"])
    if result["holdout"]:
        _print_rows("HOLDOUT (evaluated once)", list(result["holdout"].values()),
                    limit=len(result["holdout"]))
    else:
        reason = (result.get("reason")
                  or "no validation candidate cleared the frequency floor")
        print(f"\nHOLDOUT not evaluated: {reason}")

    print("\nHOLDOUT GATES")
    for gate in result["gates"]:
        print("  [%s] %-28s %s" % (
            "PASS" if gate.passed else "FAIL", gate.name, gate.detail))
    winner_candidate = result["winner_candidate"]
    suggestion = winner_candidate.config_patch(cfg) if winner_candidate else {}
    print("\nVERDICT:", "APPROVED" if result["approved"] else "NO CHANGE")
    if winner_candidate is not None:
        print("  winner:", winner_candidate.label)
        print("  patch :", suggestion or "(matches the live config)")

    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "timeframe": timeframe,
        "symbols": symbols,
        "primary_symbol": primary_symbol,
        "split": asdict(split),
        "split_dates": _split_dates(caches[symbols[0]]["df"], split),
        "costs": {key: value for key, value in costs.items()
                  if key != "start_equity"},
        "start_equity": float(costs.get("start_equity")
                              or bt.DEFAULT_EQUITY),
        "frequency_floor_per_1000_bars": args.min_trades_per_1000_bars,
        "min_holdout_pf": args.min_holdout_pf,
        "min_holdout_expectancy": args.min_holdout_expectancy,
        "candidates": len(candidates),
        "top_k": args.top_k,
        "train_passed_floor": sum(
            1 for row in result["train"]
            if passes_frequency_floor(
                (row.get("metrics") or {}).get("trades", 0),
                row.get("bars", 0), args.min_trades_per_1000_bars)),
        "train": [public_row(row) for row in result["train"]],
        "advancing": [public_row(row) for row in result["advancing"]],
        "validation": [public_row(row) for row in result["validation"]],
        "holdout": {label: public_row(row)
                    for label, row in result["holdout"].items()},
        "winner": public_row(result["winner"]),
        "baseline_holdout": public_row(result["holdout"].get(baseline.label)),
        "gates": [asdict(gate) for gate in result["gates"]],
        "approved": bool(result["approved"]),
        "suggestion": suggestion,
        "applied": False,
        "reason": result.get("reason", ""),
    }
    os.makedirs(os.path.dirname(args.out) or STATE, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1)

    if args.apply:
        if not result["approved"]:
            print("\nNO CHANGE: holdout gates failed; config.json untouched.")
        elif not suggestion:
            print("\nNO CHANGE: winner matches the live config.")
        else:
            apply_patch_file(config_path, suggestion)
            report["applied"] = True
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(report, handle, indent=1)
            print(f"\nAPPLIED: {suggestion}")
            print("backup: config.json.bak_pre_tune — restart the runner")
    else:
        print("\n(dry run — --apply is gated on every holdout gate above)")
    print(f"\nstudy -> {args.out}")


if __name__ == "__main__":
    main()
