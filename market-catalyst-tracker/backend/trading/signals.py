"""
Signal Detection Engine
========================
Detects trading edges across prediction markets. Each detector returns a
Signal dict or None. The strategy engine consumes these and decides whether
to enter a position.

EDGE CATEGORIES (from article analysis + our existing infrastructure):

1. ORDER_BOOK_IMBALANCE
   Real-time CLOB order book depth imbalance on BTC 5-minute markets.
   When bid volume >> ask volume, the market is likely going UP.
   Source: polyrec/dash.py websocket pattern

2. PRICE_DIVERGENCE
   Cross-source price divergence (Binance vs Chainlink reference price).
   Large divergences predict short-term reversion direction.
   Source: polyrec/dash.py — divergence between oracle_btc_price and binance_btc_price

3. ARBITRAGE_MACRO
   Financial-market implied probability vs Polymarket probability gap.
   e.g. Fed funds futures imply 90% rate cut → Polymarket shows 70% → BUY YES.
   This is the key unexplored edge: two separate markets pricing the same event.

4. BIOTECH_CATALYST
   FDA historical approval rate by drug type vs Polymarket approval probability.
   Historical NDA approval rate = ~85% (first-review); PDUFA with CRL history = ~55%.
   If Polymarket prices a known-type drug at 50% when history says 85% → edge.

5. NEWS_LAG
   Our news correlator detects breaking news instantly.
   Polymarket markets take 5–30 minutes to fully reprice.
   On a Fed announcement, buy the correct side before the market reprices.

6. MOMENTUM_CORRELATION
   Our momentum scanner detects stock squeezes.
   If MRNA is up 15% on high volume and there's a Polymarket FDA approval
   market at 55%, the stock is "saying" the drug likely got approved → buy YES.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx

from data.polymarket_adapter import get_order_book, get_live_midpoint, search_markets
from data.yfinance_adapter import get_quote
from analysis.news_correlator import MARKET_DRIVERS
from trading.models import SignalType, OrderSide
from trading.penny_scanner import (
    scan_penny_markets, rank_opportunities, calc_ev, calc_confidence,
    ENTRY_PRICE as PENNY_ENTRY, TAKE_PROFIT as PENNY_TP,
    TARGET_POS as PENNY_TARGET_POS,
)

logger = logging.getLogger(__name__)

# Binance price URL (used in polyrec as divergence reference)
BINANCE_BTC_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"


# ── Signal schema ──────────────────────────────────────────────────────────────

def _signal(
    signal_type: SignalType,
    side:        OrderSide,
    confidence:  float,         # 0–1
    rationale:   str,
    market_id:   str = "",
    question:    str = "",
    suggested_price: Optional[float] = None,
    suggested_size:  Optional[float] = None,  # $ amount
    metadata:    Optional[Dict] = None,
) -> Dict:
    return {
        "signal_type":     signal_type.value,
        "side":            side.value,
        "confidence":      round(confidence, 3),
        "rationale":       rationale,
        "market_id":       market_id,
        "question":        question,
        "suggested_price": suggested_price,
        "suggested_size":  suggested_size,
        "metadata":        metadata or {},
        "timestamp":       int(time.time()),
    }


# ── 1. Order Book Imbalance ────────────────────────────────────────────────────

async def detect_order_book_imbalance(
    yes_token_id: str,
    question:     str,
    market_id:    str,
    imbalance_threshold: float = 2.0,  # bid_vol / ask_vol ratio
) -> Optional[Dict]:
    """
    Detects when the YES order book shows strong directional bias.
    bid_vol >> ask_vol → market leans YES (bullish)
    ask_vol >> bid_vol → market leans NO (bearish)
    """
    book = await get_order_book(yes_token_id)
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    if not bids or not asks:
        return None

    bid_vol = sum(float(b["size"]) for b in bids[:5])
    ask_vol = sum(float(a["size"]) for a in asks[:5])

    if ask_vol == 0 or bid_vol == 0:
        return None

    ratio = bid_vol / ask_vol

    if ratio >= imbalance_threshold:
        conf = min(0.9, 0.5 + (ratio - imbalance_threshold) * 0.15)
        return _signal(
            SignalType.ORDER_BOOK_IMBALANCE, OrderSide.YES, conf,
            f"Bid/ask imbalance {ratio:.2f}× — strong YES pressure",
            market_id=market_id, question=question,
            suggested_price=0.52, suggested_size=50,
            metadata={"bid_vol": bid_vol, "ask_vol": ask_vol, "ratio": ratio},
        )
    elif ratio <= 1 / imbalance_threshold:
        conf = min(0.9, 0.5 + (1/ratio - imbalance_threshold) * 0.15)
        return _signal(
            SignalType.ORDER_BOOK_IMBALANCE, OrderSide.NO, conf,
            f"Bid/ask imbalance {1/ratio:.2f}× — strong NO pressure",
            market_id=market_id, question=question,
            suggested_price=0.52, suggested_size=50,
            metadata={"bid_vol": bid_vol, "ask_vol": ask_vol, "ratio": ratio},
        )
    return None


# ── 2. Price Divergence (Binance vs reference price) ─────────────────────────

async def detect_price_divergence(
    yes_token_id: str,
    market_id:    str,
    question:     str,
    threshold_pct: float = 0.3,  # % divergence to trigger
) -> Optional[Dict]:
    """
    Detects divergence between the Polymarket midpoint and Binance BTC price
    movement in the last tick. When BTC moves sharply but the Polymarket
    UP/DOWN market hasn't repriced, there's a short-window edge.
    (Pattern from polyrec/dash.py — divergence between oracle and Binance price)
    """
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(BINANCE_BTC_URL)
            binance_price = float(r.json().get("price", 0))
    except Exception:
        return None

    pm_midpoint = await get_live_midpoint(yes_token_id)
    if not pm_midpoint or not binance_price:
        return None

    # Implied BTC direction from PM midpoint: > 0.5 = market expects UP
    pm_bias    = pm_midpoint - 0.5   # positive = YES, negative = NO

    # We'd need the current vs previous BTC price to compute divergence fully.
    # As a proxy: if PM midpoint is neutral (0.45–0.55) but BTC moved > 0.5%
    # in the last candle, there's lag.
    # For now, just log the data — a more sophisticated strategy would
    # compare against a rolling BTC price buffer.

    if abs(pm_bias) < 0.05:
        return _signal(
            SignalType.PRICE_DIVERGENCE,
            OrderSide.YES if pm_bias >= 0 else OrderSide.NO,
            0.45,
            f"PM neutral (mid={pm_midpoint:.3f}) — monitoring for divergence; BTC=${binance_price:,.0f}",
            market_id=market_id, question=question,
            metadata={"pm_mid": pm_midpoint, "btc_price": binance_price},
        )
    return None


# ── 3. Macro Arbitrage ────────────────────────────────────────────────────────

# Historical baselines for key macro events
_MACRO_BASELINES = {
    "fed rate cut": {
        "keywords": ["fed", "rate cut", "federal reserve", "fomc", "basis points", "cut rates"],
        "description": "Historical: Fed cuts when inflation <3% + unemployment rising",
        "historical_yes_rate": None,   # depends on context — use FedWatch as reference
    },
    "recession": {
        "keywords": ["recession", "gdp negative", "economic contraction"],
        "description": "Probability calibrated against economist consensus",
        "historical_yes_rate": 0.30,
    },
    "inflation above": {
        "keywords": ["inflation", "cpi above", "cpi exceed"],
        "description": "Compare against Fed targets and current trajectory",
        "historical_yes_rate": None,
    },
}


async def detect_macro_arbitrage(
    markets: List[Dict],
) -> List[Dict]:
    """
    Scans macro Polymarket markets for gaps vs financial market implied probabilities.
    Currently identifies structurally mis-priced markets based on known baselines.
    Future enhancement: pull Fed funds futures (CME FedWatch) for exact arbitrage.
    """
    signals = []
    for m in markets:
        q = m.get("question", "").lower()
        yes_p = m.get("yes_price")
        if not yes_p:
            continue

        for event_type, info in _MACRO_BASELINES.items():
            if not any(kw in q for kw in info["keywords"]):
                continue

            hist = info["historical_yes_rate"]
            if hist is None:
                continue

            gap = hist - yes_p
            if abs(gap) > 0.15:  # > 15% gap between historical and current market
                side = OrderSide.YES if gap > 0 else OrderSide.NO
                conf = min(0.75, 0.5 + abs(gap) * 0.8)
                signals.append(_signal(
                    SignalType.ARBITRAGE_MACRO, side, conf,
                    f"Historical base rate {hist:.0%} vs PM price {yes_p:.0%} — {abs(gap):.0%} gap",
                    market_id=m.get("condition_id", ""),
                    question=m.get("question", ""),
                    suggested_price=yes_p + (0.02 if side == OrderSide.YES else -0.02),
                    suggested_size=100,
                    metadata={"historical_rate": hist, "pm_price": yes_p, "gap": gap},
                ))
    return signals


# ── 4. Biotech Catalyst Arbitrage ────────────────────────────────────────────

# FDA historical approval rates by submission type (source: FDA website)
FDA_APPROVAL_RATES = {
    "NDA":   0.82,   # New Drug Application, standard review
    "BLA":   0.78,   # Biologics License Application
    "sNDA":  0.88,   # Supplemental NDA (expanded indication)
    "PDUFA": 0.85,   # General PDUFA target action date
    "AdCom": 0.73,   # After positive AdCom vote — lower if AdCom mixed
    "CRL":   0.35,   # After Complete Response Letter (resubmission)
    "Phase3":0.58,   # Phase 3 success (precedes FDA submission)
}


async def detect_biotech_catalyst_edge(
    biotech_markets: List[Dict],
) -> List[Dict]:
    """
    Compares Polymarket biotech/FDA approval probability against historical
    FDA approval rates. If the market underprices or overprices vs history,
    this is a structural edge.

    Key insight from article: "no single strategy wins all the time" —
    we size based on confidence and apply a stop-loss.
    """
    signals = []
    for m in biotech_markets:
        q = m.get("question", "").lower()
        yes_p = m.get("yes_price")
        if not yes_p or m.get("closed"):
            continue

        # Determine which FDA rate applies
        rate = None
        event_label = ""
        if "crl" in q or "complete response" in q:
            rate = FDA_APPROVAL_RATES["CRL"]
            event_label = "Post-CRL resubmission"
        elif "adcom" in q or "advisory committee" in q:
            rate = FDA_APPROVAL_RATES["AdCom"]
            event_label = "Advisory Committee"
        elif "phase 3" in q or "phase iii" in q:
            rate = FDA_APPROVAL_RATES["Phase3"]
            event_label = "Phase 3 success"
        elif "approve" in q or "approval" in q or "nda" in q or "bla" in q:
            rate = FDA_APPROVAL_RATES["NDA"]
            event_label = "FDA Approval (NDA/BLA)"

        if rate is None:
            continue

        gap = rate - yes_p
        if abs(gap) < 0.12:  # < 12% gap — not enough edge
            continue

        side = OrderSide.YES if gap > 0 else OrderSide.NO
        conf = min(0.80, 0.45 + abs(gap) * 1.2)

        signals.append(_signal(
            SignalType.BIOTECH_CATALYST, side, conf,
            f"{event_label}: historical rate {rate:.0%} vs PM {yes_p:.0%} → {abs(gap):.0%} edge",
            market_id=m.get("condition_id", ""),
            question=m.get("question", ""),
            suggested_price=yes_p + (0.03 if side == OrderSide.YES else -0.03),
            suggested_size=150,
            metadata={
                "fda_historical_rate": rate,
                "pm_price": yes_p,
                "gap": round(gap, 4),
                "event_type": event_label,
                "volume": m.get("volume", 0),
            },
        ))

    # Sort by confidence desc
    signals.sort(key=lambda s: s["confidence"], reverse=True)
    return signals


# ── 5. News Lag Arbitrage ─────────────────────────────────────────────────────

async def detect_news_lag(
    news_items:     List[Dict],
    macro_markets:  List[Dict],
    lag_window_s:   int = 1800,   # 30 minutes — PM repricing window
) -> List[Dict]:
    """
    When breaking news appears in our news feed but the related Polymarket
    market hasn't repriced yet, there's a time-window edge.

    Logic:
    1. Take latest news items (< lag_window_s old)
    2. Classify their driver (fed_policy, inflation, geopolitical…)
    3. Find matching Polymarket macro markets
    4. Check if price has significantly moved vs pre-news expectation
       (simplified: if sentiment is strongly bearish/bullish but PM is neutral)
    """
    signals = []
    now = time.time()
    recent_news = [n for n in news_items if (now - n.get("timestamp", 0)) < lag_window_s]

    if not recent_news:
        return []

    # Aggregate sentiment by driver
    driver_sentiment: Dict[str, List[float]] = {}
    for n in recent_news:
        driver = n.get("driver_category", "other")
        sentiment = n.get("sentiment_score", 0.0)
        driver_sentiment.setdefault(driver, []).append(sentiment)

    # Average sentiment per driver
    avg_sentiment = {d: sum(scores)/len(scores) for d, scores in driver_sentiment.items()}

    for m in macro_markets:
        yes_p = m.get("yes_price")
        if not yes_p:
            continue
        q = m.get("question", "").lower()

        # Match market to driver
        matched_driver = None
        for driver, keywords in MARKET_DRIVERS.items():
            if any(kw.lower() in q for kw in keywords):
                matched_driver = driver
                break

        if not matched_driver or matched_driver not in avg_sentiment:
            continue

        sent = avg_sentiment[matched_driver]
        # If strongly bullish news but market priced below 50%, or strongly bearish but above 50%
        if sent > 0.3 and yes_p < 0.45:
            conf = min(0.70, 0.40 + sent * 0.5)
            signals.append(_signal(
                SignalType.NEWS_LAG, OrderSide.YES, conf,
                f"Strong positive {matched_driver} news (sent={sent:.2f}) but PM at {yes_p:.0%} — news lag",
                market_id=m.get("condition_id", ""),
                question=m.get("question", ""),
                suggested_price=0.50,
                suggested_size=75,
                metadata={"sentiment": sent, "driver": matched_driver, "pm_price": yes_p},
            ))
        elif sent < -0.3 and yes_p > 0.55:
            conf = min(0.70, 0.40 + abs(sent) * 0.5)
            signals.append(_signal(
                SignalType.NEWS_LAG, OrderSide.NO, conf,
                f"Strong negative {matched_driver} news (sent={sent:.2f}) but PM at {yes_p:.0%} — news lag",
                market_id=m.get("condition_id", ""),
                question=m.get("question", ""),
                suggested_price=0.50,
                suggested_size=75,
                metadata={"sentiment": sent, "driver": matched_driver, "pm_price": yes_p},
            ))

    signals.sort(key=lambda s: s["confidence"], reverse=True)
    return signals


# ── 6. Momentum Correlation ───────────────────────────────────────────────────

async def detect_momentum_correlation(
    momentum_candidates: List[Dict],   # from momentum scanner
    biotech_markets:     List[Dict],   # Polymarket biotech markets
) -> List[Dict]:
    """
    Cross-references momentum scanner output with Polymarket biotech markets.
    e.g. MRNA up 15% on 5× relative volume + SQUEEZE signal
    → search Polymarket for "Moderna" → if priced at 55%, stock is suggesting 85%
    → buy YES

    This is the key 'unexplored edge': using equity momentum as a leading
    indicator for prediction market repricing.
    """
    signals = []

    # Build a simple keyword → biotech market lookup
    keyword_markets: Dict[str, List[Dict]] = {}
    for m in biotech_markets:
        q = m.get("question", "").lower().split()
        for word in q:
            if len(word) > 4:
                keyword_markets.setdefault(word, []).append(m)

    for c in momentum_candidates:
        company = (c.get("company") or c.get("symbol", "")).lower()
        change_pct = c.get("change_pct", 0)
        signals_list = c.get("signals", [])
        rvol = c.get("relative_volume", 1)

        # Only high-conviction moves
        if abs(change_pct) < 8 or rvol < 3:
            continue

        # Find any matching PM market
        for word in company.split():
            if len(word) < 4:
                continue
            matches = keyword_markets.get(word, [])
            for m in matches:
                yes_p = m.get("yes_price")
                if not yes_p:
                    continue

                # Stock up strongly → expect YES
                if change_pct > 8:
                    stock_implied = min(0.90, yes_p + change_pct / 100)
                    gap = stock_implied - yes_p
                    if gap > 0.10:
                        conf = min(0.75, 0.45 + gap * 0.8 + (rvol - 3) * 0.02)
                        signals.append(_signal(
                            SignalType.MOMENTUM_CORRELATION, OrderSide.YES, conf,
                            f"{c['symbol']} +{change_pct:.1f}% rvol={rvol:.1f}× [{', '.join(signals_list)}] → PM may lag",
                            market_id=m.get("condition_id", ""),
                            question=m.get("question", ""),
                            suggested_price=min(yes_p + 0.05, 0.90),
                            suggested_size=100,
                            metadata={
                                "stock": c["symbol"],
                                "stock_chg_pct": change_pct,
                                "rvol": rvol,
                                "pm_price": yes_p,
                                "implied": stock_implied,
                            },
                        ))

    signals.sort(key=lambda s: s["confidence"], reverse=True)
    return signals


# ── Penny harvest detector ────────────────────────────────────────────────────

async def detect_penny_harvest(
    current_positions: int = 0,
    max_new_signals: int = 20,
) -> List[Dict]:
    """
    Scan all active markets for 1-cent contracts with positive expected value.

    Edge (from @paonx_eth dataset — 400M trades, 6 years):
      EV per $0.01 contract = +$0.0336 (verified positive)
      Portfolio EV on 50 positions × $1 each = +$206 expected per cycle

    Rules applied from the 8 winning wallets:
      - Limit orders only (no taker fees at entry)
      - No category filter (scan everything)
      - Mechanical TP at 99c (eliminate disposition effect)
      - Hold concurrently across 50 positions

    Only emits signals if we are below the TARGET_POS threshold.
    Each signal has a PENNY_HARVEST type and confidence derived from EV.
    """
    slots_available = max(0, PENNY_TARGET_POS - current_positions)
    if slots_available <= 0:
        return []

    try:
        opps = await scan_penny_markets()
    except Exception as e:
        logger.debug("Penny scan error: %s", e)
        return []

    if not opps:
        return []

    ranked = rank_opportunities(opps)
    signals = []

    for opp in ranked[:max_new_signals]:
        signals.append(_make_signal(
            signal_type = SignalType.PENNY_HARVEST,
            side        = OrderSide.YES if opp["side"] == "yes" else OrderSide.NO,
            confidence  = opp["confidence"],
            market_id   = opp["condition_id"],
            rationale   = (
                f"1c harvest: EV=+${opp['ev']:.4f}/share · "
                f"liq=${opp['liquidity']:,.0f} · "
                f"{opp['days_to_expiry']:.0f}d left · "
                f"score={opp['score']}"
            ),
            suggested_price = opp["entry_price"],
            suggested_size  = 1.0,   # $1 per position (100 shares × $0.01)
            metadata = {
                "ev":             opp["ev"],
                "ev_pct":         opp["ev_pct"],
                "token_id":       opp["token_id"],
                "slug":           opp["slug"],
                "question":       opp["question"],
                "days_to_expiry": opp["days_to_expiry"],
                "liquidity":      opp["liquidity"],
                "take_profit":    PENNY_TP,
                "score":          opp["score"],
            },
        ))

    return signals


# ── Master signal scan ────────────────────────────────────────────────────────

async def run_signal_scan(
    biotech_markets:     List[Dict] = None,
    macro_markets:       List[Dict] = None,
    news_items:          List[Dict] = None,
    momentum_candidates: List[Dict] = None,
    btc_yes_token_id:    Optional[str] = None,
    btc_market_id:       str = "",
    btc_question:        str = "Will BTC be UP in the next 5 minutes?",
    current_penny_positions: int = 0,
) -> List[Dict]:
    """
    Run all signal detectors concurrently. Returns a combined, deduplicated,
    confidence-sorted list of signals.
    """
    tasks = []

    if btc_yes_token_id:
        tasks.append(detect_order_book_imbalance(btc_yes_token_id, btc_question, btc_market_id))
        tasks.append(detect_price_divergence(btc_yes_token_id, btc_market_id, btc_question))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    signals = [r for r in results if isinstance(r, dict)]

    if macro_markets:
        signals += await detect_macro_arbitrage(macro_markets)

    if biotech_markets:
        signals += await detect_biotech_catalyst_edge(biotech_markets)

    if news_items and macro_markets:
        signals += await detect_news_lag(news_items, macro_markets)

    if momentum_candidates and biotech_markets:
        signals += await detect_momentum_correlation(momentum_candidates, biotech_markets)

    # Penny harvest — scans ALL markets, runs concurrently with other detectors
    penny_signals = await detect_penny_harvest(current_positions=current_penny_positions)
    signals += penny_signals

    # Deduplicate by (market_id, side)
    seen = set()
    unique = []
    for s in sorted(signals, key=lambda x: x["confidence"], reverse=True):
        key = (s["market_id"], s["side"])
        if key not in seen:
            seen.add(key)
            unique.append(s)

    return unique
