"""Quote quality gate shared by the runner, dashboard and diagnostics.

A broker feed can be present but unusable: a zero bid, a crossed book, a
negative spread, a frozen tick or a spread far wider than the symbol's normal
range.  Trading on such a quote is never correct, and *managing* a position on
it is worse, so every consumer must ask the same question before acting.

The gate is deliberately simple and side-effect free.  It does not fetch
anything and it never raises; callers decide whether to skip entries, skip
management, or just label the dashboard.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class QuoteQuality:
    """Result of checking one bid/ask snapshot."""

    valid: bool
    reason: str = ""
    spread_points: float = 0.0
    spread_price: float = 0.0
    age_seconds: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)


def assess_quote(
    bid: float,
    ask: float,
    point: float,
    *,
    tick_time: Optional[float] = None,
    now: Optional[float] = None,
    max_spread_points: float = 0.0,
    max_age_seconds: float = 5.0,
) -> QuoteQuality:
    """Return a structured verdict for one quote.

    Parameters
    ----------
    bid, ask:
        Current prices.  Non-positive values are invalid.
    point:
        Symbol point size (e.g. 0.01 for a 2-digit gold symbol).
    tick_time:
        Unix timestamp of the tick, when available.  ``None`` skips the
        freshness check (some historical/dataframe paths have no tick time).
    max_spread_points:
        Hard spread ceiling in MT5 points.  ``0`` disables the ceiling.
    max_age_seconds:
        Maximum acceptable tick age when ``tick_time`` is supplied.
    """
    try:
        bid = float(bid)
        ask = float(ask)
        point = float(point)
    except (TypeError, ValueError):
        return QuoteQuality(False, "non-numeric quote")

    if point <= 0:
        return QuoteQuality(False, "invalid point size")
    if bid <= 0 or ask <= 0:
        return QuoteQuality(False, f"non-positive quote (bid={bid:g}, ask={ask:g})")
    if ask < bid:
        return QuoteQuality(False, f"crossed quote (bid={bid:g} > ask={ask:g})")

    spread_price = ask - bid
    spread_points = spread_price / point
    if spread_points < 0:
        return QuoteQuality(False, "negative spread")

    age = None
    if tick_time is not None:
        try:
            age = max(0.0, float(now if now is not None else time.time())
                      - float(tick_time))
        except (TypeError, ValueError):
            age = None
        if age is not None and max_age_seconds > 0 and age > max_age_seconds:
            return QuoteQuality(
                False,
                f"stale quote ({age:.1f}s > {max_age_seconds:.1f}s)",
                spread_points, spread_price, age,
            )

    if max_spread_points > 0 and spread_points > max_spread_points:
        return QuoteQuality(
            False,
            f"spread {spread_points:.0f}pts > {max_spread_points:.0f}pts",
            spread_points, spread_price, age,
        )

    return QuoteQuality(True, "", spread_points, spread_price, age)


def spread_limit_for(symbol: str, risk_cfg: dict) -> float:
    """Resolve the spread ceiling for a symbol.

    ``max_spread_points_by_symbol`` lets metals use a wider ceiling than FX
    without loosening the FX gate.  Matching is by prefix so broker suffixes
    (``XAUUSD.a``) still find the configured entry.
    """
    limits = (risk_cfg or {}).get("max_spread_points_by_symbol") or {}
    sym = (symbol or "").upper()
    for key, value in limits.items():
        if sym.startswith(str(key).upper()):
            try:
                return float(value)
            except (TypeError, ValueError):
                break
    return float((risk_cfg or {}).get("max_spread_points", 0) or 0)

