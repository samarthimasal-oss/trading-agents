"""Research replay harness for the strategy engines.

The harness is deliberately conservative:

  * a proposal made on the close of bar ``i`` is filled at the open of bar
    ``i + 1`` — never at the close that produced it;
  * if a bar touches both the stop and the target, the stop is assumed first;
  * a gap through the stop fills at the gap open, not at the stop;
  * spread and slippage are charged on entry and exit;
  * strategy exits decided on a closed bar are filled at the next open.

It reports R-multiple statistics per strategy and combined.  It is research
tooling: the CSV fixture produces deterministic output, not a profitability
claim.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from market.data_engine import timeframe_seconds
from market.features import build_features
from strategies.arbiter import StrategyArbiter
from strategies.base import BaseStrategy, PositionView, StrategyContext, StrategySignal
from strategies.registry import build_strategies, strategy_config


@dataclass
class ReplayConfig:
    symbol: str = "XAUUSD"
    timeframe: str = "M15"
    point: float = 0.01
    digits: int = 2
    spread_points: float = 20.0
    slippage_points: float = 2.0
    warmup_bars: int = 210
    max_open_positions: int = 1
    risk_per_trade: float = 1.0

    @classmethod
    def from_config(cls, config: dict | None, **overrides) -> "ReplayConfig":
        section = strategy_config(config).get("replay") or {}
        values = {
            "symbol": (config or {}).get("trading", {}).get("symbol", "XAUUSD"),
            "timeframe": (config or {}).get("trading", {}).get("timeframe", "M15"),
        }
        for key in ("symbol", "timeframe", "point", "digits", "spread_points",
                    "slippage_points", "warmup_bars", "max_open_positions",
                    "risk_per_trade"):
            if key in section:
                values[key] = section[key]
        values.update(overrides)
        return cls(**values)


@dataclass
class StrategyTrade:
    strategy: str
    symbol: str
    timeframe: str
    direction: int
    entry_time: float
    exit_time: float
    entry: float
    exit: float
    stop: float
    target: float
    risk_price: float
    r_gross: float
    r_net: float
    mfe_r: float
    mae_r: float
    bars_held: int
    exit_reason: str
    session: str
    regime: str
    setup_id: str
    confidence: float
    spread_points: float
    cost_r: float
    bar_time: float

    @property
    def win(self) -> bool:
        return self.r_net > 0

    @property
    def month(self) -> str:
        dt = datetime.fromtimestamp(self.exit_time, tz=timezone.utc)
        return dt.strftime("%Y-%m")

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class _OpenPosition:
    signal: StrategySignal
    entry: float
    entry_time: float
    entry_index: int
    risk_price: float
    stop: float
    target: float
    mfe_r: float = 0.0
    mae_r: float = 0.0
    pending_exit: str = ""
    exit_ready_index: int = -1


@dataclass
class ReplayResult:
    trades: list[StrategyTrade] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    proposals: int = 0
    selected: int = 0
    bars: int = 0
    config: Optional[ReplayConfig] = None
    data_note: str = ""
    strategies: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ metrics
    def trades_for(self, strategy: Optional[str] = None) -> list[StrategyTrade]:
        if strategy is None:
            return list(self.trades)
        return [t for t in self.trades if t.strategy == strategy]

    def metrics(self, strategy: Optional[str] = None) -> dict:
        trades = sorted(self.trades_for(strategy), key=lambda t: (t.exit_time, t.entry_time))
        result = {
            "strategy": strategy or "combined",
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_r": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "avg_r": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "max_drawdown_r": 0.0,
            "max_consecutive_losses": 0,
            "sharpe": 0.0,
            "avg_mfe_r": 0.0,
            "avg_mae_r": 0.0,
            "exit_reasons": {},
        }
        if not trades:
            return result
        r = np.array([float(t.r_net) for t in trades], dtype=float)
        wins = r[r > 0]
        losses = r[r < 0]
        result["trades"] = int(len(r))
        result["wins"] = int(wins.size)
        result["losses"] = int(losses.size)
        result["win_rate"] = float(wins.size / len(r) * 100.0)
        result["total_r"] = float(r.sum())
        result["expectancy_r"] = float(r.mean())
        result["avg_r"] = float(r.mean())
        gross_win = float(wins.sum()) if wins.size else 0.0
        gross_loss = float(-losses.sum()) if losses.size else 0.0
        result["profit_factor"] = float(gross_win / gross_loss) if gross_loss > 0 else (
            float("inf") if gross_win > 0 else 0.0)
        result["avg_win_r"] = float(wins.mean()) if wins.size else 0.0
        result["avg_loss_r"] = float(losses.mean()) if losses.size else 0.0
        cum = np.cumsum(r)
        peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
        drawdown = peak - cum
        result["max_drawdown_r"] = float(max(0.0, drawdown.max()))
        streak = 0
        for value in r:
            if value < 0:
                streak += 1
                result["max_consecutive_losses"] = max(
                    result["max_consecutive_losses"], streak)
            else:
                streak = 0
        std = float(r.std(ddof=1)) if len(r) > 1 else 0.0
        result["sharpe"] = float(r.mean() / std * math.sqrt(len(r))) if std > 0 else 0.0
        result["avg_mfe_r"] = float(np.mean([t.mfe_r for t in trades]))
        result["avg_mae_r"] = float(np.mean([t.mae_r for t in trades]))
        result["exit_reasons"] = dict(Counter(t.exit_reason for t in trades))
        return result

    def by_strategy(self) -> dict[str, dict]:
        return {name: self.metrics(name) for name in self.strategies}

    def by_session(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for session in sorted({t.session or "unknown" for t in self.trades}):
            subset = [t for t in self.trades if (t.session or "unknown") == session]
            out[session] = _metrics_for(subset)
        return out

    def by_regime(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for regime in sorted({t.regime or "unknown" for t in self.trades}):
            subset = [t for t in self.trades if (t.regime or "unknown") == regime]
            out[regime] = _metrics_for(subset)
        return out

    def by_month(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for month in sorted({t.month for t in self.trades}):
            subset = [t for t in self.trades if t.month == month]
            out[month] = _metrics_for(subset)
        return out

    def equity_curve(self) -> list[float]:
        curve = [0.0]
        for trade in sorted(self.trades, key=lambda t: (t.exit_time, t.entry_time)):
            curve.append(curve[-1] + trade.r_net)
        return curve


def _metrics_for(trades: Iterable[StrategyTrade]) -> dict:
    """Small helper so breakdowns reuse exactly the combined metric rules."""
    items = list(trades)
    if not items:
        return {"trades": 0, "total_r": 0.0, "win_rate": 0.0,
                "expectancy_r": 0.0, "profit_factor": 0.0}
    # Reuse ReplayResult.metrics without mutating the caller's result.
    temp = ReplayResult(trades=items)
    return temp.metrics()


def _adverse(points: float, point: float) -> float:
    return max(0.0, float(points)) * float(point)


def _close_position(
    pos: _OpenPosition,
    exit_price: float,
    exit_time: float,
    index: int,
    reason: str,
    config: ReplayConfig,
    trades: list[StrategyTrade],
) -> StrategyTrade:
    half = 0.5 * float(config.spread_points) + float(config.slippage_points)
    cost_price = _adverse(half, config.point)
    adjusted_exit = exit_price - pos.signal.direction * cost_price
    gross = pos.signal.direction * (exit_price - pos.entry) / pos.risk_price
    net = pos.signal.direction * (adjusted_exit - pos.entry) / pos.risk_price
    cost_r = abs(gross - net)
    trade = StrategyTrade(
        strategy=pos.signal.strategy,
        symbol=pos.signal.symbol,
        timeframe=pos.signal.timeframe,
        direction=pos.signal.direction,
        entry_time=pos.entry_time,
        exit_time=float(exit_time),
        entry=float(pos.entry),
        exit=float(adjusted_exit),
        stop=float(pos.stop),
        target=float(pos.target),
        risk_price=float(pos.risk_price),
        r_gross=float(gross),
        r_net=float(net),
        mfe_r=float(pos.mfe_r),
        mae_r=float(pos.mae_r),
        bars_held=max(0, int(index - pos.entry_index)),
        exit_reason=reason,
        session=pos.signal.session,
        regime=pos.signal.regime,
        setup_id=pos.signal.setup_id,
        confidence=float(pos.signal.confidence),
        spread_points=float(config.spread_points),
        cost_r=float(cost_r),
        bar_time=float(pos.signal.bar_time),
    )
    trades.append(trade)
    return trade


def run_replay(
    df: pd.DataFrame,
    config: dict | None = None,
    *,
    replay: Optional[ReplayConfig] = None,
    strategies: Optional[dict[str, BaseStrategy]] = None,
    features_ready: bool = False,
    data_note: str = "",
) -> ReplayResult:
    """Replay ``df`` with the five engines (or an injected set for tests).

    ``df`` needs OHLCV and ``time`` columns.  Unless ``features_ready`` is
    true, :func:`market.features.build_features` is applied first.
    """
    cfg = replay or ReplayConfig.from_config(config)
    frame = df.copy()
    if not features_ready:
        frame = build_features(frame, (config or {}).get("market", {}).get("features"))
    if frame is None or frame.empty:
        return ReplayResult(config=cfg, data_note=data_note)

    engines = strategies if strategies is not None else build_strategies(config)
    result = ReplayResult(config=cfg, data_note=data_note,
                          strategies=list(engines.keys()))
    result.bars = int(len(frame))
    arbiter = StrategyArbiter(config)
    rejection_counter: Counter = Counter()

    start = max(int(cfg.warmup_bars), 1)
    if start >= len(frame):
        start = max(1, len(frame) - 1)

    active: dict[str, _OpenPosition] = {}
    pending: list[StrategySignal] = []
    half_cost = 0.5 * float(cfg.spread_points) + float(cfg.slippage_points)
    entry_cost = _adverse(half_cost, cfg.point)
    exit_cost = entry_cost

    for i in range(start, len(frame)):
        bar = frame.iloc[i]
        bar_time = float(bar["time"])
        open_ = float(bar["open"])
        high = float(bar["high"])
        low = float(bar["low"])

        # 1. fill proposals from the previous close at this bar's open
        for signal in pending:
            if signal.strategy in active:
                rejection_counter["position already open"] += 1
                continue
            if len(active) >= int(cfg.max_open_positions):
                rejection_counter["max open positions"] += 1
                continue
            entry = open_ + signal.direction * entry_cost
            if signal.direction > 0 and entry <= signal.stop:
                rejection_counter["gap through stop at entry"] += 1
                continue
            if signal.direction < 0 and entry >= signal.stop:
                rejection_counter["gap through stop at entry"] += 1
                continue
            risk_price = abs(entry - signal.stop)
            if risk_price <= 0:
                rejection_counter["zero risk at entry"] += 1
                continue
            active[signal.strategy] = _OpenPosition(
                signal=signal,
                entry=float(entry),
                entry_time=bar_time,
                entry_index=i,
                risk_price=float(risk_price),
                stop=float(signal.stop),
                target=float(signal.target),
            )
        pending = []

        # 2. manage open positions on this bar
        for name, pos in list(active.items()):
            direction = pos.signal.direction
            risk = pos.risk_price
            if direction > 0:
                pos.mfe_r = max(pos.mfe_r, (high - pos.entry) / risk)
                pos.mae_r = max(pos.mae_r, (pos.entry - low) / risk)
            else:
                pos.mfe_r = max(pos.mfe_r, (pos.entry - low) / risk)
                pos.mae_r = max(pos.mae_r, (high - pos.entry) / risk)

            if pos.pending_exit and i >= pos.exit_ready_index:
                _close_position(pos, open_, bar_time, i, pos.pending_exit,
                                cfg, result.trades)
                active.pop(name, None)
                continue

            exit_price = None
            reason = ""
            if direction > 0:
                if open_ <= pos.stop:
                    exit_price, reason = open_, "gap stop"
                elif open_ >= pos.target:
                    exit_price, reason = open_, "gap target"
                elif low <= pos.stop:
                    exit_price, reason = pos.stop, "stop"
                elif high >= pos.target:
                    exit_price, reason = pos.target, "target"
            else:
                if open_ >= pos.stop:
                    exit_price, reason = open_, "gap stop"
                elif open_ <= pos.target:
                    exit_price, reason = open_, "gap target"
                elif high >= pos.stop:
                    exit_price, reason = pos.stop, "stop"
                elif low <= pos.target:
                    exit_price, reason = pos.target, "target"
            if exit_price is not None:
                _close_position(pos, exit_price, bar_time, i, reason,
                                cfg, result.trades)
                active.pop(name, None)

        # 3. strategy exits and time exits decided on this close
        ctx = StrategyContext(
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
            frame=frame,
            point=cfg.point,
            digits=cfg.digits,
            spread_points=cfg.spread_points,
            index=i,
        )
        for name, pos in active.items():
            if pos.pending_exit:
                continue
            bars_held = i - pos.entry_index
            max_bars = int(engines[name].params.get("max_bars_in_trade", 0) or 0)
            if max_bars > 0 and bars_held >= max_bars:
                pos.pending_exit = "time exit"
                pos.exit_ready_index = i + 1
                continue
            view = PositionView(
                strategy=name,
                symbol=cfg.symbol,
                direction=pos.signal.direction,
                entry=pos.entry,
                stop=pos.stop,
                target=pos.target,
                opened_at=pos.entry_time,
                bars_held=bars_held,
                mfe_r=pos.mfe_r,
                mae_r=pos.mae_r,
            )
            decision = engines[name].should_exit(view, ctx)
            if decision.action == "exit":
                pos.pending_exit = decision.reason or "strategy exit"
                pos.exit_ready_index = i + 1
            elif decision.action == "trail" and decision.new_stop is not None:
                new_stop = float(decision.new_stop)
                if pos.signal.direction > 0 and pos.stop < new_stop < float(bar["close"]):
                    pos.stop = new_stop
                elif pos.signal.direction < 0 and pos.stop > new_stop > float(bar["close"]):
                    pos.stop = new_stop

        # 4. new proposals at this close, filled at the next open
        if i + 1 < len(frame):
            proposals: list[StrategySignal] = []
            for name, engine in engines.items():
                if name in active or not engine.enabled:
                    continue
                proposals.extend(engine.evaluate(ctx))
            result.proposals += len(proposals)
            arbitration = arbiter.arbitrate(proposals, now=bar_time +
                                            timeframe_seconds(cfg.timeframe))
            result.selected += len(arbitration.selected)
            for rejected in arbitration.rejected:
                rejection_counter[rejected.reason] += 1
            pending = [s for s in arbitration.selected if s.strategy not in active]

    # 5. close anything still open at the final close
    final = frame.iloc[-1]
    final_time = float(final["time"])
    final_index = len(frame) - 1
    for name, pos in list(active.items()):
        _close_position(pos, float(final["close"]), final_time, final_index,
                        "end of data", cfg, result.trades)

    result.rejections = dict(rejection_counter)
    return result


def format_report(result: ReplayResult) -> str:
    cfg = result.config or ReplayConfig()
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("STRATEGY REPLAY REPORT (R multiples, net of modelled costs)")
    lines.append("=" * 72)
    lines.append(f"data                  : {result.data_note or 'dataframe'}")
    lines.append(f"symbol/timeframe      : {cfg.symbol} {cfg.timeframe}  "
                 f"({result.bars} bars)")
    lines.append(f"fills                 : next-bar open; stop-before-target on ties")
    lines.append(f"costs                 : spread {cfg.spread_points:.1f} pts + "
                 f"slippage {cfg.slippage_points:.1f} pts per side")
    lines.append(f"proposals/selected    : {result.proposals}/{result.selected}")
    if result.rejections:
        top = sorted(result.rejections.items(), key=lambda kv: -kv[1])[:6]
        lines.append("rejections            : " +
                     ", ".join(f"{k}={v}" for k, v in top))
    lines.append("")

    header = (f"{'strategy':16s} {'trades':>6s} {'win%':>6s} {'totalR':>8s} "
              f"{'expR':>7s} {'PF':>6s} {'maxDD':>7s} {'sharpe':>7s}")
    lines.append(header)
    lines.append("-" * len(header))
    for name in result.strategies:
        m = result.metrics(name)
        lines.append(_metric_line(name, m))
    lines.append("-" * len(header))
    lines.append(_metric_line("COMBINED", result.metrics()))
    lines.append("")

    days = max(1e-9, result.bars * timeframe_seconds(cfg.timeframe) / 86_400.0)
    lines.append(f"sample                : {days:.1f} days, "
                 f"{result.metrics()['trades'] / days:.2f} executed trades/day combined")
    lines.append("frequency (trades/day): " + ", ".join(
        f"{name} {result.metrics(name)['trades'] / days:.2f}"
        for name in result.strategies))
    lines.append("")

    lines.append("By session")
    lines.append(f"{'session':16s} {'trades':>6s} {'win%':>6s} {'totalR':>8s} "
                 f"{'expR':>7s} {'PF':>6s}")
    for name, m in result.by_session().items():
        lines.append(f"{name:16s} {m['trades']:6d} {m['win_rate']:6.1f} "
                     f"{m['total_r']:8.2f} {m['expectancy_r']:7.3f} "
                     f"{_pf(m['profit_factor']):>6s}")
    lines.append("")

    if result.by_regime():
        lines.append("By regime")
        for name, m in result.by_regime().items():
            lines.append(f"{name:16s} {m['trades']:6d} {m['win_rate']:6.1f} "
                         f"{m['total_r']:8.2f} {m['expectancy_r']:7.3f} "
                         f"{_pf(m['profit_factor']):>6s}")
        lines.append("")

    if result.by_month():
        lines.append("By month")
        for name, m in result.by_month().items():
            lines.append(f"{name:16s} {m['trades']:6d} {m['win_rate']:6.1f} "
                         f"{m['total_r']:8.2f} {m['expectancy_r']:7.3f} "
                         f"{_pf(m['profit_factor']):>6s}")
        lines.append("")

    reasons = result.metrics()["exit_reasons"]
    if reasons:
        lines.append("Exit reasons: " + ", ".join(
            f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])))
    lines.append("")
    lines.append("R = planned risk per trade; equity curve is the cumulative net R.")
    if "demo_1min" in (result.data_note or ""):
        lines.append("Synthetic fixture data is a determinism check, not an edge claim.")
    else:
        lines.append("In-sample historical replay with modelled costs; "
                     "not a live performance claim.")
    lines.append("=" * 72)
    return "\n".join(lines)


def _pf(value: float) -> str:
    if value == float("inf"):
        return "inf"
    return f"{value:.2f}"


def _metric_line(name: str, m: dict) -> str:
    return (f"{name:16s} {m['trades']:6d} {m['win_rate']:6.1f} "
            f"{m['total_r']:8.2f} {m['expectancy_r']:7.3f} "
            f"{_pf(m['profit_factor']):>6s} {m['max_drawdown_r']:7.2f} "
            f"{m['sharpe']:7.2f}")


__all__ = [
    "ReplayConfig",
    "ReplayResult",
    "StrategyTrade",
    "format_report",
    "run_replay",
]
