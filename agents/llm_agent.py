"""LLM trading committee: a Gemini-powered agent that reviews the quant
consensus before execution, acting as the AI air-controller.

It degrades gracefully: if there is no key, a network issue, or a malformed
response, the trade proceeds on quant-only mode (rule-based decision stands).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import requests

from core.models import MarketContext, Consensus, AccountSnapshot, LlmReview

log = logging.getLogger("llm")

GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")

SYSTEM = """You are the risk-review agent of an automated trading system.
You receive an executed-technique summary (quant consensus), recent candles,
indicators, account state and open position info from MetaTrader 5.
Decide whether the proposed trade should be APPROVED, VETOED or RESIZED.

Guidelines:
- Veto when strong counter-evidence exists conflicting the consensus, or the
  market looks choppy/overextended IN THE SAME DIRECTION as the entry.
- Resize (size_multiplier < 1, e.g. 0.5) when the trade is plausible but risky.
- Approve otherwise. Be decisive. Approve well-formed consensus trades unless
  there is a concrete reason against them.
Respond ONLY with JSON: {"action":"approve|veto|resize","size_multiplier":1.0,
"comment":"<one sentence>"}"""
FALLBACK_MODEL = "gemini-flash-latest"


def load_api_key(llm_cfg: dict) -> Optional[str]:
    key = (llm_cfg.get("api_key") or "").strip()
    if key:
        return key
    fpath = llm_cfg.get("api_key_file") or ""
    if fpath and os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            key = (data.get("gemini_api_key") or "").strip()
            if key:
                return key
        except Exception as exc:                     # unreadable key file
            log.warning("Could not read api_key_file: %s", exc)
    return None


class LlmAgent:
    def __init__(self, llm_cfg: dict):
        self.enabled = bool(llm_cfg.get("enabled", True))
        self.model = llm_cfg.get("gemini_model") or "gemini-2.0-flash"
        self.timeout = float(llm_cfg.get("timeout_seconds") or 25)
        self.min_interval = float(llm_cfg.get("min_seconds_between_reviews") or 60)
        self.key = load_api_key(llm_cfg)
        self._last_call = 0.0
        if self.enabled and not self.key:
            log.warning("LLM agent enabled but no Gemini key found -> quant-only mode.")
            self.enabled = False

    # ---------------------------------------------------------------- context
    @staticmethod
    def _brief_market(ctx: MarketContext, df=None) -> dict:
        candles = []
        if df is not None and len(df):
            tail = df.tail(8)[["open", "high", "low", "close"]].round(5)
            tail.index = [t.strftime("%m-%d %H:%M") for t in tail.index]
            candles = [
                {"time": t, "o": float(r["open"]), "h": float(r["high"]),
                 "l": float(r["low"]), "c": float(r["close"])}
                for t, r in tail.iterrows()
            ]
        return {
            "latest_candles": candles,
            "rsi": round(float(ctx.rsi or 0), 1),
            "atr": float(ctx.atr or 0),
            "spread_points": round(ctx.spread_points, 1),
            "price": round(ctx.bid, ctx.digits),
        }

    # ---------------------------------------------------------------- review
    def review(self, ctx: MarketContext, consensus: Consensus,
               account: AccountSnapshot, open_positions: list,
               proposal: dict, now_ts: Optional[float] = None) -> LlmReview:
        """proposal = {kind, direction, volume, sl, tp, atr, entry}."""
        if consensus.direction == 0:
            return LlmReview(approved=False, action="no-trade",
                             comment="No consensus to review.", used_llm=False)
        if not self.enabled:
            return LlmReview(approved=True, action="approve",
                             comment="quant-only (LLM disabled)", used_llm=False)

        now_ts = now_ts if now_ts is not None else time.time()
        if now_ts - self._last_call < self.min_interval:
            return LlmReview(approved=True, action="approve",
                             comment="rate-limited; skipping review", used_llm=False)

        try:
            payload = {
                "symbol": ctx.symbol, "timeframe": ctx.timeframe,
                "consensus": {
                    "direction": "long" if consensus.direction > 0 else "short",
                    "strength": round(consensus.strength, 3),
                    "agents": [
                        {"name": s.name, "dir": "long" if s.direction > 0 else "short",
                         "conf": round(s.confidence, 2), "reason": s.reason}
                        for s in consensus.signals
                    ],
                },
                "proposal": {k: (round(v, 3) if isinstance(v, float) else v)
                             for k, v in proposal.items() if k != "_df"},
                "account": {"balance": round(account.balance, 2),
                            "equity": round(account.equity, 2),
                            "floating_pl": round(account.profit, 2),
                            "daily_pnl": round(account.daily_pnl, 2)},
                "open_positions": [
                    {"id": p.ticket, "dir": "long" if p.type == 0 else "short",
                     "volume": p.volume,
                     "pl": round(p.profit, 2), "sl": p.sl, "tp": p.tp}
                    for p in open_positions
                ],
                "market": self._brief_market(ctx, proposal.get("_df")),
            }

            review = self._ask(payload)
            self._last_call = now_ts
            return review
        except Exception as exc:
            log.warning("LLM review failed (%s) -> proceeding quant-only", exc)
            return LlmReview(approved=True, action="approve",
                             comment="LLM unreachable", used_llm=False,
                             error=str(exc)[:300])

    def _ask(self, payload: dict) -> LlmReview:
        url = GEMINI_URL.format(model=self.model)
        body = {
            "contents": [{
                "role": "user",
                "parts": [{"text": SYSTEM + "\n\nSTATE:\n" + json.dumps(payload)}],
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2,
                "maxOutputTokens": 512,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        r = requests.post(
            url, params={"key": self.key}, headers={"Content-Type": "application/json"},
            data=json.dumps(body), timeout=self.timeout,
        )
        if r.status_code == 404:                     # configured model retired
            log.info("model %s retired -> retry with %s", self.model, FALLBACK_MODEL)
            r = requests.post(
                GEMINI_URL.format(model=FALLBACK_MODEL), params={"key": self.key},
                headers={"Content-Type": "application/json"},
                data=json.dumps(body), timeout=self.timeout,
            )
        if r.status_code != 200:
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        text = (data.get("candidates") or [{}])[0].get("content", {}) \
            .get("parts", [{}])[0].get("text", "")
        parsed = json.loads(text)
        action = str(parsed.get("action", "approve")).lower().strip()
        mult = float(parsed.get("size_multiplier", 1.0) or 1.0)
        mult = min(max(mult, 0.0), 1.5)
        if action == "veto":
            return LlmReview(False, "veto", 0.0, str(parsed.get("comment", "")), True)
        if action == "resize":
            return LlmReview(True, "resize", mult, str(parsed.get("comment", "")), True)
        return LlmReview(True, "approve", 1.0, str(parsed.get("comment", "")), True)
