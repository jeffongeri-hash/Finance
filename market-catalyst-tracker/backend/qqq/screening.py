"""
Signal Screening — turns a validated chain + trend into candidate credit spreads.

The builder is permissive by design: it emits every structurally possible
vertical in the allowed direction so the dashboard can show WHY candidates fail.
Approval is the risk engine's job, never the builder's.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import List, Optional

from qqq.models import CreditSpread, OptionChain, Trend
from qqq.rules import FeeModel, RiskLimits

STRATEGY_FOR_TREND = {Trend.BULLISH: ("bull_put", "P"), Trend.BEARISH: ("bear_call", "C")}


def _floor_cent(x: float) -> float:
    return math.floor(round(x * 100, 6)) / 100.0


def build_candidates(chain: OptionChain, trend: Trend, limits: RiskLimits, fees: FeeModel,
                     now: datetime, max_width: float = 5.0, top_n: int = 25) -> List[CreditSpread]:
    if trend not in STRATEGY_FOR_TREND:
        return []
    strategy, right = STRATEGY_FOR_TREND[trend]
    today = now.date()
    out: List[CreditSpread] = []
    for expiry in chain.expiries():
        dte = (expiry - today).days
        if not (limits.min_dte <= dte <= limits.max_dte):
            continue
        side = chain.side(expiry, right)
        by_strike = {q.strike: q for q in side}
        for short in side:
            if short.delta is None or not (limits.short_delta_min <= abs(short.delta) <= limits.short_delta_max):
                continue
            for long in side:
                width = (short.strike - long.strike) if right == "P" else (long.strike - short.strike)
                if width <= 0 or width > max_width or long.strike not in by_strike:
                    continue
                natural = short.bid - long.ask
                out.append(CreditSpread(
                    strategy=strategy, short=short, long=long, contracts=1,
                    limit_credit=max(_floor_cent(natural), 0.0), asof=now,
                ))
    out.sort(key=lambda s: (s.max_loss(fees), -s.limit_credit))
    return out[:top_n]


def iv_preference_note(iv_context: dict) -> Optional[str]:
    """Preference only (user rule: 'prefer elevated IV when supported by credible history')."""
    if not iv_context.get("available"):
        return "IV context unavailable — no elevated-IV preference applied"
    pct = iv_context.get("percentile_1y")
    if pct is None:
        return "IV percentile unavailable"
    level = "elevated" if pct >= 50 else "subdued"
    return f"VXN {iv_context.get('vxn')} at the {pct:.0f}th 1-year percentile ({level})"
