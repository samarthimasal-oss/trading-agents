"""The orchestrator: wires data -> quant agents -> LLM committee -> risk ->
executor, runs the polling loop, and produces a per-cycle report.
"""
from __future__ import annotations

import json
import logging
import os
import time

from agents import quant
from agents import kronos_agent
from agents.llm_agent import LlmAgent
from core import kronos_score
from core.news_filter import NewsGuard
from core.notifier import Notifier
from agents.risk_agent import RiskAgent
from core.models import AccountSnapshot
from core.mt5_client import MT5Client, MT5Error
from core.state_store import StateStore
from execution.executor import Executor
from execution.guardian import Guardian

log = logging.getLogger("runner")

_KRONOS_TF_CACHE = os.path.join("state", "kronos_tf_cache.json")


class TradingAgentRunner:
    def __init__(self, cfg: dict, store: StateStore, dry_run: bool = False):
        self.cfg = cfg
        self.store = store
        self.dry_run = dry_run

        self.client = MT5Client(cfg["mt5"])
        self.risk = RiskAgent(cfg, self.client)
        self.llm = LlmAgent(cfg["llm"])
        self.executor = Executor(
            self.client,
            magic=int(cfg["trading"]["magic"]),
            risk_cfg=cfg["risk"],
            comment=cfg["trading"].get("comment", "TradingAgents"),
        )
        self.executor.store = store
        self.guardian = Guardian(cfg, self.client, store)
        self.guardian.magic = int(cfg["trading"]["magic"])
        self.kronos_cfg = dict(cfg.get("kronos") or {})
        if self.kronos_cfg.get("enabled", True):
            log.info("Kronos foundation-model forecaster enabled (%s); loading on first bar…",
                     self.kronos_cfg.get("model", "NeoQuasar/Kronos-small"))
        # economic-calendar guard (failsafe: never blocks on its own errors)
        self.news = NewsGuard(cfg.get("news") or {})
        # telegram push (disabled until bot_token configured)
        self.notify = Notifier(cfg.get("telegram") or {})
        # Kronos exit-watch: close early when the forecast flips against a position
        kx = self.kronos_cfg.get("exit_watch") or {}
        self.kronos_exit_cfg = {"enabled": bool(kx.get("enabled", True)) and self.kronos_cfg.get("enabled", True),
                                "min_atr": float(kx.get("min_atr", 0.3)),
                                "persistence": int(kx.get("persistence", 2))}
        self._kronos_exit_streak: dict[str, int] = {}
        self._kronos_tf_cache: dict[tuple, tuple] = {}   # (symbol, tf) -> (last_bar_ts, payload)
        self._kronos_cache_dirty = False
        self._load_kronos_tf_cache()                     # survive restarts (and keep grading history)
        self._board_state: dict[str, dict] = {}   # last rich board per symbol (persists Kronos forecast)
        self.kronos_runs = max(1, int(self.kronos_cfg.get("ensemble_runs", 1) or 1))
        self.kronos_min_agreement = float(self.kronos_cfg.get("min_agreement", 0.6) or 0.6)
        self.htf_confluence = float(self.kronos_cfg.get("htf_confluence", 0.0) or 0.0)

        self.symbol: str | None = None
        self._last_bar_time: float = 0.0

    # ------------------------------------------------------------------ setup
    def symbols_list(self) -> list[str]:
        t = self.cfg["trading"]
        syms = list(t.get("symbols") or [t["symbol"]])
        return syms

    def start(self) -> AccountSnapshot:
        self.client.connect()
        resolved = []
        for raw in self.symbols_list():
            sym = self.client.resolve_symbol(raw)
            resolved.append(sym)
            if sym != raw:
                log.info("Symbol %s resolved to broker symbol %s", raw, sym)
        self.symbols = resolved
        self.cfg["trading"]["symbol"] = resolved[0]
        self._last_bar_time: dict[str, float] = {}
        log.info("Trading %d symbols: %s", len(resolved), ", ".join(resolved))
        return self.client.account()

    def stop(self):
        self.client.shutdown()

    # ------------------------------------------- kronos forecast disk cache
    def _load_kronos_tf_cache(self) -> None:
        """Restore the last HTF forecast per (symbol, tf) from disk.

        Without this, a runner restart would throw away the forecast that is
        still current for the rest of the H1/H4/D1 bar — and we could no
        longer grade it against the bar that eventually closes.
        """
        try:
            with open(_KRONOS_TF_CACHE, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:                     # noqa: BLE001
            log.warning("kronos tf cache unreadable (ignored): %s", exc)
            return
        for key, item in (raw or {}).items():
            try:
                symbol, tf = key.split("|", 1)
                last_ts, payload = item["last_ts"], item["payload"]
                if isinstance(payload, dict):
                    self._kronos_tf_cache[(symbol, tf)] = (int(last_ts), payload)
            except Exception:                        # noqa: BLE001
                continue
        if self._kronos_tf_cache:
            log.info("Restored %d cached Kronos forecasts from disk", len(self._kronos_tf_cache))

    def _save_kronos_tf_cache(self) -> None:
        if not self._kronos_cache_dirty:
            return
        try:
            os.makedirs(os.path.dirname(_KRONOS_TF_CACHE) or ".", exist_ok=True)
            raw = {f"{sym}|{tf}": {"last_ts": ts, "payload": payload}
                   for (sym, tf), (ts, payload) in self._kronos_tf_cache.items()}
            tmp = _KRONOS_TF_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, default=str)
            os.replace(tmp, _KRONOS_TF_CACHE)
            self._kronos_cache_dirty = False
        except Exception:                            # noqa: BLE001
            log.warning("could not persist kronos tf cache", exc_info=True)

    # ------------------------------------------------------------------ cycle
    def cycle(self) -> dict:
        """One poll cycle. Heavy work only happens on a new closed bar."""
        try:
            return self._cycle_inner()
        except MT5Error as exc:
            log.error("MT5 error: %s", exc)
            try:
                self.client.shutdown()
            except Exception:
                pass
            return {"error": str(exc)}
        except Exception as exc:                      # keep the loop alive
            log.exception("Unexpected error in cycle: %s", exc)
            return {"error": str(exc)}

    def _cycle_inner(self) -> dict:
        t = self.cfg["trading"]
        a = self.cfg["agents"]

        # ---- guardian supervision (once per poll, account-wide)
        account = self.client.account()
        account.daily_pnl = self.client.daily_bot_profit(int(t["magic"]))
        allowed, reason, flatten = self.guardian.check()
        report = {"equity": round(account.equity, 2),
                  "daily_pnl": round(account.daily_pnl, 2)}
        if flatten and not self.dry_run:
            n_flat = self.guardian.flatten(self.executor)
            report["flattened"] = n_flat
            self.notify.trade("guard", f"daily-loss guard: flattened {n_flat} positions ({reason})")
        if not allowed:
            report["trading"] = False
            report["why"] = reason
            return report

        # ---- per-symbol cycle (data, maintenance, signals, execution)
        for symbol in self.symbols:
            rec = self._cycle_symbol(symbol, account)
            # merge into the persistent board state so light per-poll reports
            # never wipe the rich per-bar data (consensus / kronos forecast)
            self._board_state.setdefault(symbol, {}).update(rec)
            report[symbol] = rec
        self._write_live(report)
        return report

    def _cycle_symbol(self, symbol: str, account: AccountSnapshot) -> dict:
        t = self.cfg["trading"]
        a = self.cfg["agents"]
        tf = t["timeframe"]

        try:
            ctx = self.client.build_context(symbol, tf, int(t["bars_history"]))
            bars = self.client.copy_bars(symbol, tf, int(t["bars_history"]))
        except MT5Error as exc:
            return {"error": str(exc)}
        df = quant.context_with_indicators(ctx, bars, a)

        # keep garbage broker quotes out of the dashboard board: a zero/crossed
        # price or an absurd spread means "no usable feed", not "price 0.00002"
        bad_quote = (ctx.bid <= 0 or not (0 <= ctx.spread_points <= 1000))
        report = {"symbol": symbol,
                  "price": None if bad_quote else ctx.bid,
                  "atr": ctx.atr,
                  "spread": None if bad_quote else round(ctx.spread_points, 1)}

        # ---- position maintenance (every poll)
        try:
            changes = self.executor.manage(ctx)
            if changes:
                report["sl_changes"] = changes
        except MT5Error as exc:
            report["manage_error"] = str(exc)

        # ---- multi-timeframe Kronos forecasts + news status (every poll,
        # cached per TF bar close — cheap recomputes, cheap news calendar)
        if self.kronos_cfg.get("enabled", True):
            report["forecasts"] = self._extra_forecasts(symbol)
            news_blocked, news_why = self.news.impacted(symbol)
            report["news"] = news_why if news_blocked else ""

        # ---- act only once per closed bar
        forming_bar_time = bars[-1].time
        if forming_bar_time == self._last_bar_time.get(symbol):
            report["new_bar"] = False
            return report
        self._last_bar_time[symbol] = forming_bar_time
        report["new_bar"] = True
        report["action"] = ""                         # clear stale per-bar labels
        report["why"] = ""

        closed_df = df.iloc[:-1]                      # last CLOSED bar only
        if len(closed_df) < max(a["ema_slow"], a["rsi_period"], a["atr_period"]) + 5:
            report["why"] = "not enough bars"
            return report

        # ---- grade the previous closed-bar forecast against this new bar
        prev_fc = (self._board_state.get(symbol) or {}).get("forecast")
        if prev_fc:
            graded = self._grade_forecast(prev_fc, symbol, tf, closed_df)
            if graded:
                log.info("Kronos score %s %s: %s (%.2f%% realized)",
                         symbol, tf, "HIT" if graded["hit"] else "MISS",
                         graded["realized_pct"])

        # ---- volatility regime gate: dead market = no trades
        atr_now = float(closed_df["atr"].iloc[-1])
        atr_slow = float(closed_df["atr"].tail(96).median()) if len(closed_df) >= 96 else atr_now
        atr_ratio = atr_now / max(atr_slow, 1e-12)
        report["regime"] = round(atr_ratio, 2)
        dead_ratio = float(self.cfg["risk"].get("dead_market_atr_ratio", 0.62))
        if atr_ratio < dead_ratio:
            consensus = quant.build_consensus(
                [quant.Signal("regime", 0, 0, 0.0, f"atr_ratio {atr_ratio:.2f}")], 1.0)
            report["action"] = "blocked"
            report["why"] = (f"dead market: ATR {atr_now:.6f} is only "
                             f"{atr_ratio:.0%} of 96-bar median")
            self._record(symbol, "blocked", None, consensus, None,
                         report["why"], False)
            return report

        # ---- agent votes: 5 quant strategies + Kronos foundation-model forecast
        signals = [ag.analyze(closed_df, a) for ag in quant.AGENTS]
        report["forecast"] = None
        if self.kronos_cfg.get("enabled", True):
            if not getattr(self, "_kronos_src_logged", False):
                self._kronos_src_logged = True
                log.info("kronos_agent imported from %s", kronos_agent.__file__)
            k_sig = kronos_agent.forecast_signal(
                symbol, closed_df, self.kronos_cfg, atr_now,
                float(a.get("weight_kronos", 1.1)))
            payload = getattr(k_sig, "payload", None) or {}
            report["forecast"] = payload or None
            if payload:
                report.setdefault("forecasts", {})["M15"] = payload   # TF tab view
            if k_sig.direction or payload:
                log.info("Kronos %s: dir=%d conf=%.2f | %s", symbol, k_sig.direction,
                         k_sig.confidence, payload.get("summary", k_sig.reason))
            signals.append(k_sig)
        consensus = quant.build_consensus(signals, a["consensus_threshold"])
        # ---- higher-timeframe confluence: H1/H4 forecasts known at this very
        # moment nudge the M15 consensus (§ htf_confluence). Support strengthens,
        # opposition weakens; below the threshold the direction goes neutral.
        report["consensus"] = {
            "dir": consensus.direction, "strength": round(consensus.strength, 3),
            "confidence": consensus.confidence,
            "agents": {s.name: s.direction for s in consensus.signals},
        }
        if self.htf_confluence > 0 and consensus.direction != 0:
            votes = []
            for tf_name, fc in (report.get("forecasts") or {}).items():
                if str(tf_name).upper() == tf.upper() or not fc:
                    continue
                if fc.get("ood") or int(fc.get("dir") or 0) == 0:
                    continue
                if (int(fc.get("runs", 1) or 1) > 1
                        and float(fc.get("agreement", 1.0)) < self.kronos_min_agreement):
                    continue
                votes.append(1 if int(fc["dir"]) == consensus.direction else -1)
            if votes:
                net = sum(votes) / len(votes)
                before = consensus.strength
                bias = self.htf_confluence * net * (1 if before >= 0 else -1)
                new_strength = max(-1.0, min(1.0, before + bias))
                new_dir = (1 if new_strength > 0 else -1) \
                    if abs(new_strength) >= float(a["consensus_threshold"]) else 0
                consensus.strength = new_strength
                consensus.direction = new_dir
                report["consensus"].update({
                    "dir": new_dir, "strength": round(new_strength, 3),
                    "htf": {"votes": len(votes), "net": round(net, 2),
                            "applied": round(bias, 3),
                            "before": round(before, 3)},
                })
                log.info("HTF confluence %s: %d votes net %+.0f → strength %.2f → %.2f%s",
                         symbol, len(votes), net, before, new_strength,
                         " (direction dropped)" if new_dir == 0 else "")

        positions = self.executor.bot_positions()

        # ---- Kronos exit-watch: close early when the forecast turns against
        # the open position by at least min_atr, for `persistence` bars in a row
        if self.kronos_exit_cfg["enabled"] and report.get("forecast"):
            fc = report["forecast"]
            fdir = int(fc.get("dir") or 0)
            move_ = float(fc.get("move") or 0.0)
            fmove_atr = abs(move_) / max(atr_now, 1e-12) if atr_now else 0.0
            for pos in list(positions):
                if pos.symbol != symbol:
                    continue
                pdir = 1 if pos.type == 0 else -1
                key = str(pos.ticket)
                against = (fdir != 0 and not fc.get("ood")
                           and fdir != pdir
                           and fmove_atr >= self.kronos_exit_cfg["min_atr"])
                streak = self._kronos_exit_streak.get(key, 0) + 1 if against else 0
                self._kronos_exit_streak[key] = streak if against else 0
                self._kronos_exit_streak = {k: v for k, v in self._kronos_exit_streak.items()
                                            if v > 0}      # garbage-collect stale tickets
                if against and streak >= self.kronos_exit_cfg["persistence"]:
                    if self.dry_run:
                        log.info("DRY-RUN would Kronos-exit #%s (fdir=%d move=%.2f ATR)",
                                 pos.ticket, fdir, fmove_atr)
                    else:
                        try:
                            self.executor.close_position(pos)
                            self.notify.trade(
                                "close", f"{symbol} Kronos exit: ticket {pos.ticket} "
                                f"{('LONG' if pdir > 0 else 'SHORT')} closed early — "
                                f"forecast flipped {'UP' if fdir > 0 else 'DOWN'} "
                                f"{fmove_atr:.2f} ATR")
                            log.info("Kronos exit closed #%s after %d confirming bars",
                                     pos.ticket, streak)
                        except Exception as exc:            # never kill the loop
                            log.warning("Kronos exit close failed: %s", exc)
                    self._kronos_exit_streak.pop(key, None)
                elif against:
                    log.info("Kronos exit-watch %s #%s: %.2f ATR against (%d/%d)",
                             symbol, pos.ticket, fmove_atr, streak,
                             self.kronos_exit_cfg["persistence"])

        # ---- exit on opposite consensus (this symbol only)
        if t.get("close_on_opposite", True) and consensus.direction != 0:
            for pos in list(positions):
                if pos.symbol != symbol:
                    continue
                pos_dir = 1 if pos.type == 0 else -1   # POSITION_TYPE_BUY == 0
                if pos_dir != consensus.direction:
                    if self.dry_run:
                        log.info("DRY-RUN would close opposite #%s", pos.ticket)
                    else:
                        self.executor.close_position(pos)
                        self.notify.trade("close",
                                          f"{symbol} closed ticket {pos.ticket} "
                                          f"(opposite consensus)")
                        positions = self.executor.bot_positions()

        if consensus.direction == 0:
            report["action"] = "flat"
            report["why"] = f"no consensus (strength {consensus.strength:+.2f})"
            self._record(symbol, "none", plan=None, consensus=consensus,
                         review=None, reason="no consensus", executed=False)
            return report

        # ---- news protection: no NEW trades around high-impact releases
        # (exits above are still allowed — closing is always permitted)
        news_blocked, news_why = self.news.impacted(symbol)
        if news_blocked:
            report["action"] = "blocked"
            report["why"] = news_why
            self._record(symbol, "blocked", None, consensus, None, news_why, False)
            return report

        gate = self.risk.pre_trade_gate(ctx, account, positions)
        if not gate.ok:
            report["action"] = "blocked"
            report["why"] = gate.reason
            self._record(symbol, "blocked", None, consensus, None, gate.reason, False)
            return report

        # ---- AI committee review
        proposal = {
            "kind": "market_order",
            "direction": "long" if consensus.direction > 0 else "short",
            "sl_atr_mult": a["atr_sl_mult"], "tp_atr_mult": a["atr_tp_mult"],
            "atr": ctx.atr, "price": round(ctx.bid, ctx.digits),
            "_df": closed_df,
        }
        if report.get("forecast"):
            proposal["kronos_forecast"] = report["forecast"].get("summary")
        review = self.llm.review(ctx, consensus, account, positions, proposal)
        report["llm"] = {"used": review.used_llm, "action": review.action,
                         "comment": review.comment}
        if not review.approved and t.get("require_llm_approval", False):
            report["action"] = "vetoed-by-llm"
            self._record(symbol, "vetoed", None, consensus, review,
                         review.comment or "LLM veto", False)
            return report

        # ---- risk sizing + execution
        plan = self.risk.build_plan(ctx, closed_df, a, consensus, review, account)
        executed = False
        if plan.action in ("buy", "sell") and plan.volume > 0:
            if self.dry_run:
                log.info("DRY-RUN plan: %s", plan.summary())
                report["action"] = f"dry-run {plan.action}"
            else:
                executed = self.executor.place(plan)
                report["action"] = plan.action if executed else "order-failed"
        else:
            report["action"] = "skipped (zero volume)"
        report["plan"] = {"volume": plan.volume, "sl": plan.sl, "tp": plan.tp}
        self._record(symbol, plan.action, plan, consensus, review,
                     plan.reason or "generated", executed)
        return report

    # ------------------------------------------------- multi-timeframe forecasts
    def _grade_forecast(self, payload: dict, symbol: str, timeframe: str, df) -> dict | None:
        """Grade a forecast once the bar *after* its base bar has closed."""
        try:
            if not payload or df is None or df.empty:
                return None
            base_ts = float(payload.get("base_ts") or 0.0)
            ts = float(df.index[-1].timestamp())
            if base_ts <= 0 or ts <= base_ts:
                return None
            if len(df.index) >= 2:
                step = float((df.index[-1] - df.index[-2]).total_seconds())
            else:
                step = ts - base_ts
            # must be exactly the next bar, otherwise the outcome is not recent
            if not (0.5 * step <= (ts - base_ts) <= 1.5 * step):
                return None
            return kronos_score.grade(payload, symbol, timeframe,
                                      float(df["close"].iloc[-1]), ts)
        except Exception:                            # noqa: BLE001
            log.warning("kronos grading failed for %s %s", symbol, timeframe,
                        exc_info=True)
            return None

    def _extra_forecasts(self, symbol: str) -> dict:
        """Kronos forecasts on the higher timeframes (H1/H4/D1 from config).

        Refreshed only when that timeframe's bar closes — a cheap per-poll
        bar-time check plus a cached payload. The forecast made from the
        previous bar is graded against the bar that just closed. Failsafe
        per timeframe, and the cache survives restarts on disk.
        """
        if not self.kronos_cfg.get("enabled", True):
            return {}
        out = {}
        active_tf = str(self.cfg["trading"].get("timeframe", "M15")).upper()
        for tf in (self.kronos_cfg.get("timeframes") or []):
            tf = str(tf).upper()
            if not tf or tf == active_tf:
                continue
            try:
                candles = self.client.copy_bars(symbol, tf, 460)   # lookback 384 + buffer
                if len(candles) < 90:
                    continue
                df = quant.to_frame(candles).dropna(subset=["open", "high", "low", "close"])
                df = df.iloc[:-1]                                  # closed bars only
                if df.empty:
                    continue
                if "volume" not in df.columns:
                    df["volume"] = df.get("tick_volume", 0.0)
                last_ts = int(df.index[-1].timestamp())
                key = (symbol, tf)
                cache = self._kronos_tf_cache.get(key)
                if cache and cache[0] == last_ts:
                    payload = cache[1]
                else:
                    if cache and cache[1]:                     # new bar → score old call
                        graded = self._grade_forecast(cache[1], symbol, tf, df)
                        if graded:
                            log.info("Kronos score %s %s: %s (%.2f%% realized)",
                                     symbol, tf,
                                     "HIT" if graded["hit"] else "MISS",
                                     graded["realized_pct"])
                    payload = kronos_agent.forecast(
                        df, self.kronos_cfg, runs=self.kronos_runs) or {}
                    self._kronos_tf_cache[key] = (last_ts, payload)
                    self._kronos_cache_dirty = True
                    if payload:
                        log.info("Kronos %s %s: dir=%s agree=%.2f | %s", symbol, tf,
                                 payload.get("dir"), float(payload.get("agreement", 1)),
                                 payload.get("summary", "")[:96])
                if payload:
                    out[tf] = payload
            except Exception as exc:                           # noqa: BLE001
                log.warning("kronos %s %s forecast failed (skipped): %s", symbol, tf, exc)
        self._save_kronos_tf_cache()
        return out


    # ------------------------------------------------------------------ live board
    def _write_live(self, report: dict) -> None:
        """Persist a compact per-symbol signal board for the dashboard."""
        try:
            os.makedirs("state", exist_ok=True)
            board = {}
            for symbol in self.symbols:
                # prefer the merged board state: light per-poll reports update
                # only fresh fields (price/atr/spread) and never wipe the rich
                # per-bar data (consensus / LLM verdict / Kronos forecast)
                rep = self._board_state.get(symbol) or report.get(symbol)
                if not isinstance(rep, dict):
                    continue
                board[symbol] = {
                    "price": rep.get("price"),
                    "atr": rep.get("atr"),
                    "spread": rep.get("spread"),
                    "regime": rep.get("regime"),
                    "dir": (rep.get("consensus") or {}).get("dir", 0),
                    "strength": (rep.get("consensus") or {}).get("strength"),
                    "conf": (rep.get("consensus") or {}).get("confidence"),
                    "agents": (rep.get("consensus") or {}).get("agents", {}),
                    "llm": (rep.get("llm") or {}).get("action"),
                    "llm_comment": (rep.get("llm") or {}).get("comment"),
                    "action": rep.get("action", ""),
                    "why": rep.get("why", ""),
                    "kronos_dir": (rep.get("forecast") or {}).get("dir"),
                    "kronos_summary": (rep.get("forecast") or {}).get("summary"),
                    "kronos_agreement": (rep.get("forecast") or {}).get("agreement"),
                    "kronos_runs": (rep.get("forecast") or {}).get("runs"),
                    "forecast_candles": (rep.get("forecast") or {}).get("candles"),
                    "forecasts": rep.get("forecasts") or {},
                    "htf": (rep.get("consensus") or {}).get("htf"),
                    "news": rep.get("news") or "",
                }
            payload = {"ts": time.time(),
                       "mode": "dry-run" if self.dry_run else "LIVE",
                       "symbols": board,
                       "equity": report.get("equity"),
                       "daily_pnl": report.get("daily_pnl")}
            tmp = os.path.join("state", "live.json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, default=str)
            os.replace(tmp, os.path.join("state", "live.json"))
        except Exception:
            log.exception("live board write failed")

    # ------------------------------------------------------------------ journal
    def _record(self, symbol, action, plan, consensus, review, reason, executed):
        try:
            self.store.decision(
                symbol=symbol, action=action,
                direction=consensus.direction if consensus else 0,
                volume=plan.volume if plan else 0.0,
                sl=plan.sl if plan else 0.0, tp=plan.tp if plan else 0.0,
                strength=consensus.strength if consensus else 0.0,
                confidence=consensus.confidence if consensus else 0.0,
                llm_action=(review.action if review else "n/a"),
                llm_comment=(review.comment or "") if review else "",
                reason=reason, executed=bool(executed),
            )
        except Exception:
            log.exception("journal write failed")
        # telegram push (failure-safe, never interferes with trading)
        try:
            if executed and plan is not None:
                conf_txt = f" | conf {consensus.confidence:.2f}" if consensus else ""
                self.notify.trade(
                    "fill", f"<b>{symbol}</b> {str(action).upper()} "
                    f"{plan.volume} lots | SL {plan.sl} TP {plan.tp}{conf_txt}")
            elif action == "vetoed":
                self.notify.trade("reject", f"{symbol} LLM veto: {reason}")
        except Exception:
            pass

    # ------------------------------------------------------------------ loop
    def run_forever(self):
        poll = float(self.cfg["trading"].get("poll_seconds") or 15)
        log.info("Running live loop (%s, new-bar driven, poll=%ss). Ctrl+C to stop.",
                 "DRY-RUN" if self.dry_run else "LIVE", poll)
        notice = 0
        while True:
            try:
                reset_flag = os.path.join("state", "reset_guardian.flg")
                if os.path.exists(reset_flag):           # dashboard-driven reset
                    self.guardian.reset()
                    os.remove(reset_flag)
                rep = self.cycle()
                if rep.get("new_bar"):
                    log.info("BAR %s", {k: v for k, v in rep.items() if k != "error"})
                elif rep.get("error") and time.time() - notice > 60:
                    log.warning("error: %s", rep["error"])
                    notice = time.time()
                time.sleep(poll)
            except KeyboardInterrupt:
                log.info("Interrupted by user")
                break
