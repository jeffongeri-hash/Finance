"""
Equity Strategy Engine
========================
Aggregates signals from multiple data sources and produces concrete
BUY / SELL / HOLD decisions with entry/exit conditions.

Signal sources (in priority order):
  1. Momentum scanner   — relative volume, gap, 52-week high proximity
  2. Social sentiment   — Adanos (Reddit/X/news/Polymarket buzz + bullish%)
  3. Technical          — 20-day MA, RSI, trend direction
  4. News catalyst      — primary driver category, sentiment score

Entry conditions (ALL must be met):
  • composite_score >= ENTRY_MIN_SCORE      (default 30)
  • relative_volume >= ENTRY_MIN_RVOL       (default 1.5)
  • price above 20-day MA                  (if enough history)
  • sentiment != "bearish"                 (avoid confirmed selling pressure)
  • no GAP_DOWN signal                     (not a falling knife)

Exit conditions (ANY triggers exit):
  • price < entry × (1 - STOP_LOSS_PCT)    (stop loss, default -6%)
  • price > entry × (1 + TAKE_PROFIT_PCT)  (take profit, default +18%)
  • trailing stop activates after +TRAIL_TRIGGER_PCT gain
    and trails by TRAIL_PCT from peak
  • held > MAX_HOLD_DAYS trading days
  • sentiment turns "bearish" AND score drops below EXIT_SCORE_FLOOR

Position sizing:
  • Risk RISK_PCT of account per trade using the stop-loss distance
  • Hard cap: max MAX_POSITION_PCT of account per position
  • Max MAX_OPEN_POSITIONS open positions simultaneously
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Strategy parameters (tune here or load from .env) ─────────────────────────

ENTRY_MIN_SCORE    = 25.0   # composite momentum score
ENTRY_MIN_RVOL     = 1.5    # relative volume vs 3-month average
ENTRY_MAX_RSI      = 72.0   # don't buy overbought
ENTRY_REQUIRE_MA   = True   # price must be above 20-day MA

EXIT_STOP_LOSS_PCT       = 0.06   # -6% hard stop
EXIT_TAKE_PROFIT_PCT     = 0.18   # +18% target
EXIT_TRAIL_TRIGGER_PCT   = 0.10   # trailing stop activates at +10%
EXIT_TRAIL_PCT           = 0.07   # trail 7% from peak
EXIT_MAX_HOLD_DAYS       = 7      # time stop
EXIT_SCORE_FLOOR         = 12.0   # exit if score drops this low
EXIT_ON_BEARISH          = True   # exit on confirmed bearish sentiment

RISK_PCT            = 0.01   # risk 1% of account per trade
MAX_POSITION_PCT    = 0.07   # hard cap: 7% of account per position
MAX_OPEN_POSITIONS  = 10     # max simultaneous positions
DAILY_LOSS_LIMIT_PCT = 0.02  # halt engine if day P&L < -2% of account

MIN_PRICE = 1.0    # skip penny stocks below $1
MAX_PRICE = 2000.0 # skip extremely high-priced without fractional shares

STRATEGY_VERSION = "v1.0"


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class CompositeSignal:
    """Aggregated signal for a single symbol across all data sources."""
    symbol:           str
    timestamp:        int = field(default_factory=lambda: int(time.time()))

    # Momentum
    momentum_score:   float = 0.0
    rvol:             float = 1.0
    gap_pct:          float = 0.0
    change_pct:       float = 0.0
    from_52w_high_pct: float = -100.0
    momentum_signals: List[str] = field(default_factory=list)

    # Social
    sentiment:        str   = "neutral"   # bullish / bearish / neutral / mixed
    buzz_score:       Optional[float] = None
    bullish_pct:      Optional[float] = None
    sources_agree:    bool  = False

    # Technical
    price:            float = 0.0
    above_20ma:       bool  = False
    rsi:              Optional[float] = None
    trend:            str   = "unknown"   # up / down / sideways

    # News
    news_driver:      str   = "other"
    news_sentiment:   float = 0.0        # -1 bearish → +1 bullish

    # Decision
    action:           str   = "HOLD"     # BUY / HOLD / AVOID
    entry_allowed:    bool  = False
    reasons:          List[str] = field(default_factory=list)
    blockers:         List[str] = field(default_factory=list)
    confidence:       float = 0.0        # 0–1

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ExitSignal:
    """Describes why a position should be exited."""
    symbol:       str
    reason:       str    # STOP_LOSS / TAKE_PROFIT / TRAILING / TIME / SIGNAL_REVERSAL / MANUAL
    urgency:      str    # IMMEDIATE / NEXT_OPEN / OPTIONAL
    current_price: float = 0.0
    entry_price:   float = 0.0
    pnl_pct:       float = 0.0
    detail:        str   = ""


# ── Signal aggregator ─────────────────────────────────────────────────────────

def compute_composite_signal(
    symbol: str,
    momentum_data: Optional[Dict] = None,
    sentiment_data: Optional[Dict] = None,
    technical_data: Optional[Dict] = None,
    news_data: Optional[Dict] = None,
) -> CompositeSignal:
    """
    Build a CompositeSignal from pre-fetched data dicts.
    All data inputs are optional — the signal degrades gracefully.
    """
    sig = CompositeSignal(symbol=symbol)

    # ── 1. Momentum ───────────────────────────────────────────────────────────
    if momentum_data:
        sig.price             = momentum_data.get("price", 0.0)
        sig.momentum_score    = momentum_data.get("score", 0.0)
        sig.rvol              = momentum_data.get("relative_volume", 1.0)
        sig.gap_pct           = momentum_data.get("gap_pct", 0.0)
        sig.change_pct        = momentum_data.get("change_pct", 0.0)
        sig.from_52w_high_pct = momentum_data.get("from_52w_high_pct", -100.0)
        sig.momentum_signals  = momentum_data.get("signals", [])

    # ── 2. Social sentiment ────────────────────────────────────────────────────
    if sentiment_data:
        comp = sentiment_data.get("composite", {})
        sig.sentiment     = comp.get("sentiment", "neutral")
        sig.buzz_score    = comp.get("buzz_score")
        sig.bullish_pct   = comp.get("bullish_pct")
        sig.sources_agree = comp.get("sources_agree", False)

    # ── 3. Technical (20-day MA, RSI) ─────────────────────────────────────────
    if technical_data:
        ma20    = technical_data.get("ma_20")
        rsi_val = technical_data.get("rsi")
        price   = sig.price or technical_data.get("price", 0)
        sig.price       = price
        sig.above_20ma  = bool(ma20 and price > ma20)
        sig.rsi         = float(rsi_val) if rsi_val else None
        sig.trend       = technical_data.get("trend", "unknown")

    # ── 4. News ───────────────────────────────────────────────────────────────
    if news_data:
        sig.news_driver    = news_data.get("primary_driver", "other")
        # Average sentiment across top news items
        items = news_data.get("news", [])
        if items:
            sig.news_sentiment = sum(
                n.get("sentiment_score", 0) for n in items[:5]
            ) / min(len(items), 5)

    # ── Entry decision ────────────────────────────────────────────────────────
    _evaluate_entry(sig)
    return sig


def _evaluate_entry(sig: CompositeSignal) -> None:
    """Populate entry_allowed, action, reasons, blockers, confidence."""
    reasons:  List[str] = []
    blockers: List[str] = []

    # Price range check
    if sig.price < MIN_PRICE:
        blockers.append(f"Price ${sig.price:.2f} below minimum ${MIN_PRICE}")
    elif sig.price > MAX_PRICE:
        blockers.append(f"Price ${sig.price:.2f} above maximum ${MAX_PRICE}")

    # Momentum score
    if sig.momentum_score >= ENTRY_MIN_SCORE:
        reasons.append(f"Momentum score {sig.momentum_score:.0f} ≥ {ENTRY_MIN_SCORE}")
    else:
        blockers.append(f"Momentum score {sig.momentum_score:.0f} < {ENTRY_MIN_SCORE}")

    # Relative volume
    if sig.rvol >= ENTRY_MIN_RVOL:
        reasons.append(f"Volume {sig.rvol:.1f}× average")
    else:
        blockers.append(f"Low volume {sig.rvol:.1f}× < {ENTRY_MIN_RVOL}×")

    # Social sentiment — only blocks if confirmed bearish
    if sig.sentiment == "bearish" and sig.sources_agree:
        blockers.append("Social sentiment: confirmed bearish across sources")
    elif sig.sentiment == "bullish":
        reasons.append(f"Social sentiment bullish ({(sig.bullish_pct or 0)*100:.0f}%)")
    elif sig.buzz_score and sig.buzz_score >= 50:
        reasons.append(f"High social buzz score {sig.buzz_score:.0f}/100")

    # Technical: 20-day MA
    if ENTRY_REQUIRE_MA and sig.above_20ma is False and sig.trend != "unknown":
        blockers.append("Price below 20-day moving average")
    elif sig.above_20ma:
        reasons.append("Price above 20-day MA")

    # RSI overbought
    if sig.rsi and sig.rsi > ENTRY_MAX_RSI:
        blockers.append(f"RSI {sig.rsi:.0f} overbought (> {ENTRY_MAX_RSI})")
    elif sig.rsi:
        reasons.append(f"RSI {sig.rsi:.0f} acceptable")

    # Gap down — avoid falling knives
    if "GAP_DOWN" in sig.momentum_signals:
        blockers.append("GAP_DOWN signal — avoid catching falling knife")

    # News driver alignment
    bearish_drivers = {"banking", "geopolitical"}
    if sig.news_driver in bearish_drivers and sig.news_sentiment < -0.3:
        blockers.append(f"Bearish news driver: {sig.news_driver}")

    sig.reasons  = reasons
    sig.blockers = blockers
    sig.entry_allowed = len(blockers) == 0 and len(reasons) >= 2

    # Confidence: ratio of positive conditions met (0–1)
    total_checks = len(reasons) + len(blockers)
    sig.confidence = round(len(reasons) / total_checks, 2) if total_checks > 0 else 0.0

    sig.action = "BUY" if sig.entry_allowed else ("AVOID" if blockers else "HOLD")


# ── Exit evaluator ────────────────────────────────────────────────────────────

def evaluate_exit(
    symbol:       str,
    entry_price:  float,
    current_price: float,
    peak_price:   float,
    entry_time:   float,    # unix timestamp
    entry_score:  float,
    current_score: float,
    current_sentiment: str,
    hold_days:    float,
) -> Optional[ExitSignal]:
    """
    Evaluate whether an open position should be exited.
    Returns ExitSignal if exit is warranted, None if hold.
    """
    if entry_price <= 0 or current_price <= 0:
        return None

    pnl_pct = (current_price - entry_price) / entry_price

    # 1. Hard stop loss
    if pnl_pct <= -EXIT_STOP_LOSS_PCT:
        return ExitSignal(
            symbol=symbol, reason="STOP_LOSS", urgency="IMMEDIATE",
            current_price=current_price, entry_price=entry_price,
            pnl_pct=round(pnl_pct * 100, 2),
            detail=f"Loss {pnl_pct*100:.1f}% hit stop of -{EXIT_STOP_LOSS_PCT*100:.0f}%",
        )

    # 2. Take profit
    if pnl_pct >= EXIT_TAKE_PROFIT_PCT:
        return ExitSignal(
            symbol=symbol, reason="TAKE_PROFIT", urgency="NEXT_OPEN",
            current_price=current_price, entry_price=entry_price,
            pnl_pct=round(pnl_pct * 100, 2),
            detail=f"Gain {pnl_pct*100:.1f}% hit target +{EXIT_TAKE_PROFIT_PCT*100:.0f}%",
        )

    # 3. Trailing stop (activates once up TRAIL_TRIGGER_PCT from entry)
    peak_gain = (peak_price - entry_price) / entry_price if peak_price > 0 else 0
    if peak_gain >= EXIT_TRAIL_TRIGGER_PCT:
        trail_price = peak_price * (1 - EXIT_TRAIL_PCT)
        if current_price <= trail_price:
            return ExitSignal(
                symbol=symbol, reason="TRAILING_STOP", urgency="IMMEDIATE",
                current_price=current_price, entry_price=entry_price,
                pnl_pct=round(pnl_pct * 100, 2),
                detail=f"Trailing stop: peak {peak_price:.2f} → trail floor {trail_price:.2f}",
            )

    # 4. Time stop
    if hold_days >= EXIT_MAX_HOLD_DAYS:
        return ExitSignal(
            symbol=symbol, reason="TIME_STOP", urgency="NEXT_OPEN",
            current_price=current_price, entry_price=entry_price,
            pnl_pct=round(pnl_pct * 100, 2),
            detail=f"Held {hold_days:.1f} days ≥ max {EXIT_MAX_HOLD_DAYS}",
        )

    # 5. Signal reversal (exit if fundamentals deteriorate significantly)
    if EXIT_ON_BEARISH and current_sentiment == "bearish" and current_score < EXIT_SCORE_FLOOR:
        return ExitSignal(
            symbol=symbol, reason="SIGNAL_REVERSAL", urgency="NEXT_OPEN",
            current_price=current_price, entry_price=entry_price,
            pnl_pct=round(pnl_pct * 100, 2),
            detail=f"Sentiment bearish + score {current_score:.0f} below floor {EXIT_SCORE_FLOOR}",
        )

    return None   # hold


# ── Position sizing ────────────────────────────────────────────────────────────

def calc_position_size(
    account_value: float,
    entry_price:   float,
    stop_pct:      float = EXIT_STOP_LOSS_PCT,
) -> Dict:
    """
    Calculate how many shares to buy using risk-based sizing.
    Risk RISK_PCT of account value on the trade (based on stop distance).
    Caps at MAX_POSITION_PCT of account.

    Returns {"shares": int, "dollar_amount": float, "risk_amount": float}
    """
    if account_value <= 0 or entry_price <= 0:
        return {"shares": 0, "dollar_amount": 0, "risk_amount": 0}

    risk_dollars   = account_value * RISK_PCT
    stop_dollars   = entry_price * stop_pct
    risk_shares    = int(risk_dollars / stop_dollars) if stop_dollars > 0 else 0

    max_shares     = int((account_value * MAX_POSITION_PCT) / entry_price)
    shares         = min(risk_shares, max_shares)
    shares         = max(shares, 0)

    return {
        "shares":        shares,
        "dollar_amount": round(shares * entry_price, 2),
        "risk_amount":   round(shares * stop_dollars, 2),
        "risk_pct":      round(RISK_PCT * 100, 1),
        "max_position_pct": round(MAX_POSITION_PCT * 100, 1),
    }


# ── Full scan: find tradeable candidates ──────────────────────────────────────

async def scan_for_entries(
    top_n: int = 20,
    use_sentiment: bool = True,
) -> List[CompositeSignal]:
    """
    Run the full pipeline:
      1. Momentum scan → get candidates with score ≥ threshold
      2. For each: fetch social sentiment (if Adanos key set)
      3. Evaluate entry conditions
      4. Return ranked list of BUY-eligible candidates

    This is the bot's "what should I buy right now?" function.
    """
    import asyncio
    from analysis.momentum_scanner import run_momentum_scan
    from data.yfinance_adapter import get_momentum_data

    # Step 1: momentum scan
    scan = await run_momentum_scan(top_n=50, min_score=10.0)
    candidates = scan.candidates

    signals: List[CompositeSignal] = []

    for cand in candidates:
        mom_data = {
            "price":             cand.price,
            "score":             cand.score,
            "relative_volume":   cand.relative_volume,
            "gap_pct":           cand.gap_pct or 0,
            "change_pct":        cand.change_pct,
            "from_52w_high_pct": cand.from_52w_high_pct,
            "signals":           cand.signals,
        }

        # Step 2: social sentiment (optional)
        sent_data = None
        if use_sentiment:
            try:
                from config import ADANOS_API_KEY
                if ADANOS_API_KEY:
                    from data.adanos_adapter import get_sentiment_snapshot
                    loop = asyncio.get_event_loop()
                    sent_data = await loop.run_in_executor(
                        None, get_sentiment_snapshot, cand.symbol, 3
                    )
            except Exception:
                pass

        # Step 3: compute composite signal
        sig = compute_composite_signal(
            symbol=cand.symbol,
            momentum_data=mom_data,
            sentiment_data=sent_data,
        )
        signals.append(sig)

    # Sort: BUY first, then by confidence desc
    signals.sort(key=lambda s: (s.action != "BUY", -s.confidence, -s.momentum_score))
    return signals[:top_n]
