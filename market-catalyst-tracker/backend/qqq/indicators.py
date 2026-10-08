"""Pure technical-indicator functions over closing-price lists (no look-ahead: index i uses data ≤ i)."""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

from qqq.models import Bar, Trend


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    """Wilder RSI on the last value."""
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for a, b in zip(values[:-1], values[1:]):
        ch = b - a
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def atr(bars: Sequence[Bar], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs: List[float] = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


def historical_vol(values: Sequence[float], period: int = 20) -> Optional[float]:
    if len(values) < period + 1:
        return None
    rets = [math.log(b / a) for a, b in zip(values[-period - 1:-1], values[-period:])]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def percentile_rank(history: Sequence[float], current: float) -> Optional[float]:
    """Share of historical observations strictly below `current`, 0–100."""
    clean = [h for h in history if h is not None and not math.isnan(h)]
    if len(clean) < 20:
        return None
    return round(100.0 * sum(1 for h in clean if h < current) / len(clean), 1)


def classify_trend(price: float, sma50: Optional[float], sma200: Optional[float]) -> tuple[Trend, str]:
    """User rule: bullish only above BOTH averages, bearish only below BOTH; anything else ambiguous."""
    if sma50 is None or sma200 is None:
        return Trend.AMBIGUOUS, "insufficient history for 50/200-day averages"
    if price > sma50 and price > sma200:
        return Trend.BULLISH, f"price {price:.2f} > SMA50 {sma50:.2f} and SMA200 {sma200:.2f}"
    if price < sma50 and price < sma200:
        return Trend.BEARISH, f"price {price:.2f} < SMA50 {sma50:.2f} and SMA200 {sma200:.2f}"
    return Trend.AMBIGUOUS, (f"price {price:.2f} between SMA50 {sma50:.2f} and SMA200 {sma200:.2f} "
                             "(or equal to one) — no trade")
