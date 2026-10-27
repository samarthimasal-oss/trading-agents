"""Kronos foundation-model agent: forecasts future candlesticks from history.

Kronos (shiyu-coder/Kronos, AAAI 2026) is a decoder-only transformer pre-trained
on K-lines from 45+ exchanges. We feed the recent closed bars from MT5 and get a
predicted OHLCV path for the next N candles. That forecast becomes:

  * an extra weighted vote in the quant consensus (Signal),
  * context for the LLM committee ("foundation model expects..."),
  * a dashed future-candle overlay on the dashboard.

Everything is failure-safe: if the model cannot download/load/infer, the agent
returns a neutral signal and the rest of the system trades on.
"""
from __future__ import annotations

import logging
import os
import sys
import threading

import numpy as np
import pandas as pd

from core.models import Signal

log = logging.getLogger("kronos")

_HINTS = {
    "kronos_hint": ("Kronos (foundation model for K-lines) predicts the next "
                    "candles from raw OHLCV history — data-driven, no indicators."),
    "kronos_why": "diversified deep-learning view of the near future",
}

_VENDOR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendors", "Kronos")

_lock = threading.Lock()
_state: dict = {"tried": False, "predictor": None, "error": None}


def get_predictor(kcfg: dict):
    """Lazy-load KronosPredictor once per process. Never raises; returns None on failure."""
    with _lock:
        if _state["tried"]:
            return _state["predictor"]
        _state["tried"] = True
        try:
            sys.path.insert(0, _VENDOR)
            import torch                    # noqa: F401  (import check)
            from model import Kronos, KronosTokenizer, KronosPredictor

            device = kcfg.get("device") or ("cuda" if torch.cuda.is_available() else "cpu")
            tok = KronosTokenizer.from_pretrained(
                kcfg.get("tokenizer", "NeoQuasar/Kronos-Tokenizer-base"))
            mdl = Kronos.from_pretrained(kcfg.get("model", "NeoQuasar/Kronos-small"))
            _state["predictor"] = KronosPredictor(
                mdl, tok, device=device,
                max_context=int(kcfg.get("max_context", 512)))
            log.info("Kronos forecaster ready: model=%s device=%s",
                     kcfg.get("model", "NeoQuasar/Kronos-small"), device)
        except Exception as exc:                      # noqa: BLE001 - stay live without it
            _state["error"] = str(exc)[:200]
            log.error("Kronos unavailable (trading continues without it): %s", exc)
    return _state["predictor"]


def forecast(df: pd.DataFrame, kcfg: dict) -> dict:
    """Forecast the next ``pred_len`` candles for symbol ``symbol``.

    ``df``: closed bars, DatetimeIndex, columns open/high/low/close[/volume].
    Returns a payload dict or {} on any failure:
      dir, move_atr, move_pct, candles [[ts,o,h,l,c],...], summary
    """
    predictor = get_predictor(kcfg)
    if predictor is None:
        return {}
    try:
        lookback = int(kcfg.get("lookback", 384))
        pred_len = int(kcfg.get("pred_len", 24))
        hist = df.dropna(subset=["open", "high", "low", "close"]).tail(lookback)
        if len(hist) < 60:
            return {}

        cols = ["open", "high", "low", "close"]
        if "volume" in hist.columns:
            cols.append("volume")
        x_df = hist[cols].copy()
        if "volume" not in x_df:
            x_df["volume"] = 0.0
        x_df["volume"] = x_df["volume"].fillna(0.0)

        ts = pd.Series(pd.to_datetime(hist.index))
        step = (ts.iloc[-1] - ts.iloc[-2]) if len(ts) > 1 else pd.Timedelta(15, "min")
        fut = pd.Series(pd.date_range(ts.iloc[-1] + step, periods=pred_len, freq=step))

        pred = predictor.predict(
            df=x_df.reset_index(drop=True),
            x_timestamp=ts, y_timestamp=fut, pred_len=pred_len,
            T=float(kcfg.get("T", 1.0)), top_p=float(kcfg.get("top_p", 0.9)),
            sample_count=int(kcfg.get("sample_count", 1)), verbose=False,
        )

        last_close = float(hist["close"].iloc[-1])
        pc = pred["close"].astype(float)
        move = float(pc.iloc[-1] - last_close)
        move_next = float(pc.iloc[0] - last_close)
        path_hi, path_lo = float(pc.max() - last_close), float(pc.min() - last_close)

        candles = [[int(t.timestamp()), float(r["open"]), float(r["high"]),
                    float(r["low"]), float(r["close"])]
                   for t, r in pred.assign(open=pred["open"], high=pred["high"],
                                           low=pred["low"], close=pred["close"]).iterrows()]

        horizon_s = pred_len * float(step.total_seconds()) if step else 0
        horizon_h = horizon_s / 3600.0
        # NOTE: path_hi/path_lo are absolute price deltas — express them as a
        # percentage OF THE CLOSE for the display (for XAUUSD at 4010 a +2.5
        # dollars path is only +0.06%, not "+250%").
        rel_hi = path_hi / max(last_close, 1e-9)
        rel_lo = path_lo / max(last_close, 1e-9)
        # out-of-distribution guard: a sane multi-hour FX/metal forecast never
        # implies a double-digit-percent swing. If it does, the model is
        # extrapolating noise — flag it so the vote abstains while the candles
        # still show on the dashboard.
        rel_path = max(abs(rel_hi), abs(rel_lo))
        ood = rel_path > float(kcfg.get("max_path_move_pct", 0.10))
        if ood:
            log.warning("Kronos path out-of-distribution: rel %.1f%% > cap %.0f%% — vote will abstain",
                        rel_path * 100, float(kcfg.get("max_path_move_pct", 0.10)) * 100)
        summary = (f"Kronos sees price {'UP' if move > 0 else 'DOWN'} "
                   f"{abs(move) / max(last_close, 1e-9) * 100:.2f}% over ~{horizon_h:.1f}h "
                   f"(path {rel_hi:+.2%}..{rel_lo:+.2%} vs close); "
                   f"next bar {'up' if move_next > 0 else 'down'} "
                   f"{abs(move_next) / max(last_close, 1e-9) * 100:.02f}%" +
                   ("  ⚠ OOD path — vote abstained" if ood else ""))

        return {
            "dir": 1 if move > 0 else -1,
            "move": move, "move_next": move_next,
            "path_hi": path_hi, "path_lo": path_lo,
            "rel_path": rel_path, "ood": ood,
            "candles": candles, "summary": summary,
            "lookback": len(hist), "pred_len": pred_len,
        }
    except Exception as exc:                          # noqa: BLE001
        log.error("Kronos forecast failed: %s", exc)
        return {}


def forecast_signal(symbol: str, df: pd.DataFrame, kcfg: dict,
                    atr_val: float, weight: float) -> Signal:
    """Vote Signal for the consensus from a fresh Kronos forecast."""
    payload = forecast(df, kcfg)
    sig = Signal("kronos", 0, weight, 0.05, "kronos unavailable")
    if not payload:
        return sig
    sig = Signal("kronos", 0, weight, 0.05, "kronos forecast computed")   # payload carrier
    sig.payload = payload                             # dashboard overlay & LLM context
    # out-of-distribution path (model extrapolating noise): forecast is shown
    # on the dashboard but the vote abstains — never veto real trades with it
    if payload.get("ood"):
        sig.reason = (f"Kronos path OOD ({payload['rel_path']:+.0%} vs close "
                      f"in {payload['pred_len']} bars) — abstained")
        return sig
    move_atr = payload["move"] / atr_val if atr_val and atr_val > 0 else 0.0
    direction = payload["dir"] if abs(move_atr) >= float(kcfg.get("min_move_atr", 0.25)) else 0
    conf = float(np.clip(abs(move_atr) / 1.5, 0.05, 0.85))
    why = (f"Kronos: {'+' if direction > 0 else ''}"
           f"{move_atr:+.2f} ATR over {payload['pred_len']} bars "
           f"(path {payload['path_lo']:+.3f}..{payload['path_hi']:+.3f} vs close)")
    sig = Signal("kronos", direction, weight, conf, why)
    sig.payload = payload                             # dashboard overlay & LLM context
    return sig
