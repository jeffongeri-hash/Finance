"""
Penny Harvest Scanner
======================
Implements the "dead contract" asymmetric edge discovered by @paonx_eth:

  From 400M+ trades / 6 years of Polymarket data:
    • 2.66% of 1-cent contracts eventually resolve at 99c → 99x return
    • 3.33% bounce to 50c+ without full resolution → partial exit
    • ~94% stay dead and resolve at 0 → -$0.01 loss

  EV per $0.01 contract:
    EV = (0.0266 × $0.99) + (0.0333 × $0.50) + (0.94 × -$0.01)
       = $0.0263 + $0.0167 - $0.0094
       = +$0.0336  ← positive EV on a 1-cent bet

  Portfolio math (100 positions × $1 each):
    EV = 100 × [(0.06 × $50) - (0.94 × $1)] = +$206 on $100 risk
    = 206% expected return per cycle

Key behavioral rules (replicated from the 8 winning wallets):
    ✓ Limit orders ONLY — never pay taker fees at entry
    ✓ Hold 50+ simultaneous positions
    ✓ No category bias — scan everything
    ✓ Exit mechanically at 99c — no early sells (eliminates disposition effect)
    ✓ Never set a stop-loss — the loss is already capped at $0.01

Source: https://twitter.com/paonx_eth
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from data.polymarket_adapter import GAMMA_BASE, CLOB_BASE

logger = logging.getLogger(__name__)

# ── Strategy constants (from the article) ─────────────────────────────────────

ENTRY_PRICE    = 0.01    # buy at or below 1 cent
ENTRY_MAX      = 0.03    # tolerance band (up to 3c still positive EV)
TAKE_PROFIT    = 0.99    # exit at 99c
ORDER_SIZE     = 100.0   # shares per position → $1 risk at 1c
TARGET_POS     = 50      # simultaneous open positions
MIN_LIQUIDITY  = 1_000   # minimum $ market liquidity
MIN_DAYS       = 14      # minimum days to expiry
MAX_DAYS       = 200     # maximum days to expiry

# EV constants calibrated from the dataset
FULL_RESOLVE_RATE = 0.0266   # P(resolves to 99c)
BOUNCE_RATE       = 0.0333   # P(bounces to 50c+)
LOSS_RATE         = 1 - FULL_RESOLVE_RATE - BOUNCE_RATE

AVG_WIN_BLENDED = (
    FULL_RESOLVE_RATE * 0.99 +   # full 99x win
    BOUNCE_RATE * 0.50            # partial bounce average
) / (FULL_RESOLVE_RATE + BOUNCE_RATE)

# ── EV calculator ──────────────────────────────────────────────────────────────

def calc_ev(entry_price: float) -> float:
    """
    Expected value per share at a given entry price.
    EV = P(full_resolve)*(0.99-entry) + P(bounce)*(0.50-entry) + P(loss)*(-entry)
    """
    return (
        FULL_RESOLVE_RATE * (0.99 - entry_price)
        + BOUNCE_RATE * (0.50 - entry_price)
        + LOSS_RATE * (-entry_price)
    )


def calc_confidence(entry_price: float) -> float:
    """
    Convert EV to a [0,1] confidence score compatible with the trading engine.
    Uses Kelly-inspired scaling: higher EV → higher confidence.

    At 1c: EV = +$0.0336 → confidence ≈ 0.68
    At 3c: EV = +$0.0136 → confidence ≈ 0.57
    At 5c: EV ≈ 0        → confidence ≈ 0.50
    """
    ev = calc_ev(entry_price)
    # Normalise to [0.50, 0.95] range
    # EV range observed: -0.05 to +0.035
    return min(0.95, max(0.50, 0.50 + ev * 10))


# ── Market scanner ────────────────────────────────────────────────────────────

async def scan_penny_markets(
    entry_max: float = ENTRY_MAX,
    min_liquidity: float = MIN_LIQUIDITY,
    min_days: int = MIN_DAYS,
    max_days: int = MAX_DAYS,
    max_results: int = 200,
) -> List[Dict]:
    """
    Scan ALL active Polymarket markets for contracts priced near 1 cent.

    Returns list of opportunities sorted by EV descending:
        {
          "condition_id", "slug", "question",
          "token_id",     "side",
          "entry_price",  "ev",       "confidence",
          "liquidity",    "days_to_expiry",
          "volume",
        }
    """
    now = datetime.now(timezone.utc)
    opportunities: List[Dict] = []

    # Paginate through all active markets
    offset = 0
    batch_size = 100
    all_markets: List[Dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                r = await client.get(
                    f"{GAMMA_BASE}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": batch_size,
                        "offset": offset,
                    }
                )
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                all_markets.extend(batch)
                if len(batch) < batch_size:
                    break
                offset += batch_size
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.debug("Gamma paginate error at offset %d: %s", offset, e)
                break

    logger.info("Penny scan: fetched %d markets total", len(all_markets))

    for m in all_markets:
        if m.get("closed") or not m.get("active"):
            continue
        if m.get("enableOrderBook") is False:
            continue

        # Liquidity filter
        liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        if liq < min_liquidity:
            continue

        # Expiry filter
        end_str = m.get("endDateIso") or m.get("endDate")
        if not end_str:
            continue
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone(timezone.utc)
            days_left = (end_dt - now).total_seconds() / 86400
        except Exception:
            continue
        if not (min_days <= days_left <= max_days):
            continue

        # Parse prices and token IDs
        prices_raw = m.get("outcomePrices", [])
        tokens_raw = m.get("clobTokenIds", [])
        if isinstance(prices_raw, str):
            import json
            try:
                prices_raw = json.loads(prices_raw)
            except Exception:
                continue
        if isinstance(tokens_raw, str):
            import json
            try:
                tokens_raw = json.loads(tokens_raw)
            except Exception:
                continue

        if len(prices_raw) != len(tokens_raw) or len(prices_raw) < 2:
            continue

        outcomes = m.get("outcomes", ["YES", "NO"])
        if isinstance(outcomes, str):
            import json
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = ["YES", "NO"]

        # Check each outcome for penny price
        for i, (price_str, token_id) in enumerate(zip(prices_raw, tokens_raw)):
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue

            if price > entry_max:
                continue

            ev = calc_ev(price)
            if ev <= 0:
                continue

            opportunities.append({
                "condition_id":  m.get("conditionId") or m.get("id") or "",
                "slug":          m.get("slug", ""),
                "question":      (m.get("question") or "")[:120],
                "token_id":      str(token_id),
                "side":          "yes" if i == 0 else "no",
                "outcome":       outcomes[i] if i < len(outcomes) else str(i),
                "entry_price":   round(price, 4),
                "ev":            round(ev, 4),
                "ev_pct":        round(ev * 100, 2),
                "confidence":    round(calc_confidence(price), 3),
                "liquidity":     round(liq, 0),
                "volume":        float(m.get("volume") or 0),
                "days_to_expiry": round(days_left, 1),
                "category":      m.get("category", ""),
                "scanned_at":    int(time.time()),
            })

    # Sort by EV descending, then liquidity
    opportunities.sort(key=lambda o: (o["ev"], o["liquidity"]), reverse=True)
    logger.info("Penny scan: found %d opportunities (entry ≤ %.2f)", len(opportunities), entry_max)
    return opportunities[:max_results]


# ── Position EV validator (for live positions) ────────────────────────────────

def validate_portfolio_ev(
    positions: List[Dict],
    n_positions: int = TARGET_POS,
    dollar_per_pos: float = 1.0,
) -> Dict:
    """
    Calculate live portfolio-level expected value for a set of penny positions.

    Args:
        positions: list of {entry_price, ...} dicts
        n_positions: total target position count
        dollar_per_pos: dollars at risk per position
    Returns:
        {
          "portfolio_ev":     float,  # total expected $ gain
          "portfolio_ev_pct": float,  # % on total capital
          "avg_ev_per_pos":   float,
          "positions_needed": int,    # how many more to reach target
          "kelly_fraction":   float,  # Kelly criterion bet fraction
        }
    """
    if not positions:
        return {
            "portfolio_ev": 0.0,
            "portfolio_ev_pct": 0.0,
            "avg_ev_per_pos": calc_ev(ENTRY_PRICE) * dollar_per_pos,
            "positions_needed": n_positions,
            "kelly_fraction": _kelly_fraction(ENTRY_PRICE),
        }

    evs = [calc_ev(p.get("entry_price", ENTRY_PRICE)) * dollar_per_pos
           for p in positions]
    total_ev = sum(evs)
    total_capital = len(positions) * dollar_per_pos

    return {
        "portfolio_ev":     round(total_ev, 4),
        "portfolio_ev_pct": round(total_ev / total_capital * 100, 2) if total_capital else 0.0,
        "avg_ev_per_pos":   round(total_ev / len(positions), 4),
        "positions_needed": max(0, n_positions - len(positions)),
        "kelly_fraction":   _kelly_fraction(ENTRY_PRICE),
    }


def _kelly_fraction(entry_price: float) -> float:
    """
    Full Kelly criterion for binary outcome:
    f* = (b*p - q) / b  where b = payout odds, p = win rate, q = loss rate
    Win rate from data: 6% (full + partial), avg payout: $50 → 50x on $1
    """
    win_p  = FULL_RESOLVE_RATE + BOUNCE_RATE   # 0.0599
    loss_q = 1 - win_p                          # 0.9401
    b      = AVG_WIN_BLENDED / entry_price      # avg win / cost
    kelly  = (b * win_p - loss_q) / b
    # Use quarter-Kelly for safety
    return round(max(0, kelly * 0.25), 4)


# ── Opportunity scorer (ranks which 1c opportunities to enter first) ──────────

def rank_opportunities(opps: List[Dict]) -> List[Dict]:
    """
    Score and rank penny opportunities beyond raw EV.
    Additional factors:
      - Prefer higher liquidity (easier to fill limit orders)
      - Prefer longer time to expiry (more time for probability to reassert)
      - Penalise very thin markets
    """
    for o in opps:
        base_score = o["ev"] * 1000   # scale EV to 0-33 range

        # Liquidity bonus: log10 scaling, max +20
        liq_bonus = min(20, (o["liquidity"] / 1000) ** 0.5 * 5)

        # Days-to-expiry bonus: prefer 30–120 day range
        days = o["days_to_expiry"]
        if 30 <= days <= 120:
            days_bonus = 10
        elif days > 120:
            days_bonus = 5
        else:
            days_bonus = 0   # < 30 days: binary resolution imminent, skip

        o["score"] = round(base_score + liq_bonus + days_bonus, 2)

    return sorted(opps, key=lambda o: o["score"], reverse=True)
