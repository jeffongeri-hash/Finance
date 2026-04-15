"""
Momentum / Exponential Move Scanner
=====================================
Screens the configured universe for stocks showing signs of an imminent or
ongoing exponential move. Signals detected:

  BREAKOUT   — price trading above its 52-week high on elevated volume
  GAP_UP     — gapped > 3% at open vs prior close on high relative volume
  SQUEEZE    — high short interest + rising price (short squeeze setup)
  HIGH_VOL   — relative volume > 3× average (unusual accumulation)
  GAP_DOWN   — gapped down > 5% (for short-side awareness)

Scoring (0–100 composite):
  • Relative volume:        up to 30 pts
  • Gap magnitude:          up to 25 pts
  • 52-week high proximity: up to 20 pts
  • Short squeeze setup:    up to 25 pts
  • Price change %:         up to 15 pts   (caps at 100 total)
"""
from __future__ import annotations
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from config import MOMENTUM_UNIVERSE
from data.yfinance_adapter import get_momentum_data
from models.schemas import MomentumCandidate, ScanResult

logger = logging.getLogger(__name__)

_THREAD_WORKERS = 4    # reduced for cloud deployment (512MB RAM limit)


# ── Scoring ────────────────────────────────────────────────────────────────────

def _score(data: Dict) -> float:
    score = 0.0

    # 1. Relative volume (most predictive of continuation)
    rvol = data.get("relative_volume", 1.0)
    if rvol >= 8:   score += 30
    elif rvol >= 5: score += 22
    elif rvol >= 3: score += 14
    elif rvol >= 2: score += 7

    # 2. Gap magnitude
    gap = abs(data.get("gap_pct", 0))
    if gap >= 15:   score += 25
    elif gap >= 10: score += 18
    elif gap >= 5:  score += 12
    elif gap >= 3:  score += 6

    # 3. 52-week high proximity (0 = at high, negative = below)
    h_pct = data.get("from_52w_high_pct", -100)
    if h_pct >= 0:    score += 20   # at or above 52w high — breakout
    elif h_pct >= -3: score += 14
    elif h_pct >= -8: score += 7

    # 4. Short squeeze potential
    si = data.get("short_interest_pct") or 0
    chg = data.get("change_pct", 0)
    if si >= 30 and chg > 5:  score += 25
    elif si >= 20 and chg > 3: score += 16
    elif si >= 10 and chg > 5: score += 8

    # 5. Raw price change % on the day
    if chg >= 20:   score += 15
    elif chg >= 10: score += 10
    elif chg >= 5:  score += 5

    return min(score, 100.0)


def _detect_signals(data: Dict) -> List[str]:
    signals: List[str] = []
    rvol = data.get("relative_volume", 1.0)
    gap = data.get("gap_pct", 0)
    h_pct = data.get("from_52w_high_pct", -100)
    si = data.get("short_interest_pct") or 0
    chg = data.get("change_pct", 0)

    if h_pct >= -2 and rvol >= 2:
        signals.append("BREAKOUT")
    if gap >= 3:
        signals.append("GAP_UP")
    elif gap <= -5:
        signals.append("GAP_DOWN")
    if si >= 15 and chg > 3:
        signals.append("SQUEEZE")
    if rvol >= 3 and "BREAKOUT" not in signals:
        signals.append("HIGH_VOL")

    return signals or ["MOMENTUM"]


def _build_candidate(data: Dict) -> MomentumCandidate:
    return MomentumCandidate(
        symbol=data["symbol"],
        company=data["company"],
        price=data["price"],
        change_pct=data["change_pct"],
        relative_volume=data["relative_volume"],
        gap_pct=data.get("gap_pct"),
        from_52w_high_pct=data["from_52w_high_pct"],
        short_interest_pct=data.get("short_interest_pct"),
        float_shares=data.get("float_shares"),
        signals=_detect_signals(data),
        score=round(_score(data), 1),
    )


# ── Main scanner ───────────────────────────────────────────────────────────────

def _fetch_one(symbol: str) -> Optional[Dict]:
    """Thread worker: fetch momentum data for one symbol, return None on failure."""
    try:
        return get_momentum_data(symbol)
    except Exception as e:
        logger.debug("momentum_data %s: %s", symbol, e)
        return None


async def run_momentum_scan(
    universe: Optional[List[str]] = None,
    top_n: int = 30,
    min_score: float = 20.0,
    min_rvol: float = 1.5,
) -> ScanResult:
    """
    Full momentum scan.
    Returns top_n candidates sorted by score (descending).
    """
    tickers = universe or MOMENTUM_UNIVERSE
    logger.info("Momentum scan starting: %d tickers", len(tickers))

    loop = asyncio.get_event_loop()
    raw_results: List[Dict] = []

    # Run in thread pool (yfinance is sync)
    with ThreadPoolExecutor(max_workers=_THREAD_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, sym): sym for sym in tickers}
        completed_futures = as_completed(futures, timeout=60)
        for fut in completed_futures:
            result = fut.result()
            if result:
                raw_results.append(result)

    # Filter and score
    candidates: List[MomentumCandidate] = []
    for data in raw_results:
        if (
            data.get("relative_volume", 0) >= min_rvol
            or abs(data.get("gap_pct", 0)) >= 3
            or (data.get("from_52w_high_pct", -999) >= -5)
        ):
            c = _build_candidate(data)
            if c.score >= min_score:
                candidates.append(c)

    # Sort by score descending
    candidates.sort(key=lambda x: x.score, reverse=True)

    logger.info("Momentum scan complete: %d candidates from %d tickers", len(candidates), len(tickers))
    return ScanResult(
        candidates=candidates[:top_n],
        scanned=len(raw_results),
        timestamp=int(time.time()),
    )


async def run_squeeze_scan(top_n: int = 20) -> ScanResult:
    """
    Focused short-squeeze scan:
    Filters for symbols with short interest > 15% and positive momentum.
    """
    tickers = MOMENTUM_UNIVERSE
    loop = asyncio.get_event_loop()
    raw_results: List[Dict] = []

    with ThreadPoolExecutor(max_workers=_THREAD_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, sym): sym for sym in tickers}
        for fut in as_completed(futures, timeout=60):
            result = fut.result()
            if result and (result.get("short_interest_pct") or 0) >= 15:
                raw_results.append(result)

    candidates = [_build_candidate(d) for d in raw_results if d.get("change_pct", 0) > 0]
    candidates.sort(key=lambda x: (x.short_interest_pct or 0), reverse=True)

    return ScanResult(
        candidates=candidates[:top_n],
        scanned=len(MOMENTUM_UNIVERSE),
        timestamp=int(time.time()),
    )
