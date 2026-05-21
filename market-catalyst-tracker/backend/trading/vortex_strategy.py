"""
VORTEX-1: Volatility-Anchored Momentum with Sentiment Exhaustion Reversal
==========================================================================
Citadel-style quant strategy — implemented for IBKR Client Portal Gateway.

Entry (ALL 5 required on 1H confirmed candle close):
  1. TREND_ALIGNMENT     — price > 200-EMA (1H) AND daily price > 50-SMA
  2. MOMENTUM_EXHAUSTION — RSI(14) dipped < 35 within last 3 bars, now rising
  3. VOLUME_CONFIRM      — current 1H volume >= 1.8× 20-bar average
  4. VOLATILITY_WINDOW   — VIX 18–38, HV Rank (IVR proxy) 30–70%
  5. PRICE_STRUCTURE     — price at prior support (±0.5%) OR hammer/engulfing

Time filter: 9:45–11:30 AM and 2:00–3:45 PM EST only. No FOMC/CPI.

Exit (first trigger wins):
  TP1 (+1.5R) → close 50%, move stop to breakeven
  TP2 (+2.5R / +3R bull / +1.5R bear) → close remaining 50%
  Hard stop   (-1R, below signal candle low)
  Time stop   (3:45 PM EST)
  Trailing    (1H candle low trail, activates after TP1)

Position sizing:
  Risk 2% per trade ($20 on $1K). Max 20% stocks / 10% options.
  Max 2 concurrent. Always >= 60% cash.
  Circuit breakers: 3 consecutive losses = halt day; -10% account = halve sizes.

Market regime (SPY vs 50D/200D SMA + VIX):
  BULL — SPY > both MAs, VIX < 20  → long only, TP2 = 3R, prefer calls
  BEAR — SPY < both MAs, VIX > 25  → short/cash, size ×0.5, TP2 = 1.5R
  CHOP — between                   → max 1 trade/day, highest conviction only
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Universe ──────────────────────────────────────────────────────────────────
TRADEABLE_UNIVERSE: List[str] = [
    "SPY", "QQQ", "IWM",                        # Tier 1: index ETFs
    "NVDA", "AAPL", "TSLA", "AMZN", "META",      # Tier 3: mega-cap
    "XLK", "XLE", "SOXX",                        # Tier 4: sector ETFs
]
REGIME_WATCH: List[str] = ["UVXY", "SQQQ"]      # read-only vol signals
VIX_SYMBOL  = "^VIX"
SPY_SYMBOL  = "SPY"

# ── Parameters ────────────────────────────────────────────────────────────────
EMA_1H       = 200
SMA_DAILY    = 50
RSI_PERIOD   = 14
RSI_OVERSOLD = 35.0
RSI_LOOKBACK = 3          # bars to look back for RSI dip
VOL_MULT     = 1.8        # volume confirmation multiplier
VOL_AVG_BARS = 20
VIX_MIN      = 18.0
VIX_MAX      = 38.0
IVR_MIN      = 30.0
IVR_MAX      = 70.0
SUPPORT_TOL  = 0.005      # ±0.5% for support proximity

RISK_PCT        = 0.02    # risk 2% of account per trade
MAX_STOCK_PCT   = 0.20    # max 20% per stock position
MAX_OPT_PCT     = 0.10    # max 10% per options position
MAX_CONCURRENT  = 2
MIN_CASH_PCT    = 0.60
CONSEC_LOSS_HALT = 3

TP1_R            = 1.5
TP2_R_NORMAL     = 2.5
TP2_R_BULL       = 3.0    # bull regime extension
TP2_R_BEAR       = 1.5    # bear regime tighten

# Trade windows in (hhmm, hhmm) format
WINDOW_AM    = (945, 1130)
WINDOW_PM    = (1400, 1545)
TIME_STOP_HM = 1545


# ── Enums + dataclasses ───────────────────────────────────────────────────────

class MarketRegime(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    CHOP = "CHOP"


@dataclass
class VortexSignal:
    symbol:         str
    action:         str              # "BUY_LONG" | "SELL_SHORT" | "PASS"
    regime:         str
    score:          int              # 0–5 filters passed
    filters_passed: List[str]
    filters_failed: List[str]
    entry_price:    Optional[float] = None
    stop_price:     Optional[float] = None
    tp1_price:      Optional[float] = None
    tp2_price:      Optional[float] = None
    r_dollar:       Optional[float] = None   # $-value of 1R
    shares:         Optional[int]   = None
    current_rsi:    Optional[float] = None
    current_volume: Optional[float] = None
    avg_volume:     Optional[float] = None
    vix:            Optional[float] = None
    ivr:            Optional[float] = None
    timestamp:      float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VortexPosition:
    symbol:       str
    entry_price:  float
    stop_price:   float
    tp1_price:    float
    tp2_price:    float
    r_dollar:     float
    shares:       float
    half_closed:  bool  = False
    stop_be:      bool  = False    # stop moved to breakeven after TP1
    trail_active: bool  = False
    trail_low:    float = 0.0
    entry_time:   float = field(default_factory=time.time)


@dataclass
class VortexExitSignal:
    symbol:  str
    action:  str    # "CLOSE_HALF" | "CLOSE_ALL" | "HOLD"
    reason:  str
    urgency: str    # "IMMEDIATE" | "EOD" | "NORMAL"
    price:   float

    def to_dict(self) -> dict:
        return asdict(self)


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d    = s.diff()
    gain = d.clip(lower=0)
    loss = (-d).clip(lower=0)
    ag   = gain.ewm(com=n - 1, adjust=False).mean()
    al   = loss.ewm(com=n - 1, adjust=False).mean()
    rs   = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _hv_rank(close: pd.Series, hv_window: int = 21, lookback: int = 252) -> float:
    """
    Historical Volatility Rank as a proxy for IV Rank.
    Returns 0–100; mid=50 means current HV is at the median of its 52-week range.
    """
    log_ret = np.log(close / close.shift(1)).dropna()
    hv = log_ret.rolling(hv_window).std() * np.sqrt(252) * 100
    hv = hv.dropna()
    if len(hv) < 5:
        return 50.0
    recent = hv.iloc[-min(lookback, len(hv)):]
    lo, hi = float(recent.min()), float(recent.max())
    if hi <= lo:
        return 50.0
    return float(((hv.iloc[-1] - lo) / (hi - lo)) * 100)


def _is_hammer(o: float, h: float, l: float, c: float) -> bool:
    body         = abs(c - o)
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    return body > 0 and lower_shadow >= 2 * body and upper_shadow <= 0.5 * body


def _is_bullish_engulfing(po: float, pc: float, co: float, cc: float) -> bool:
    return pc < po and cc > co and co < pc and cc > po


def _find_supports(lows: pd.Series, lookback: int = 60) -> List[float]:
    arr = lows.iloc[-lookback:].values if len(lows) > lookback else lows.values
    levels: List[float] = []
    for i in range(2, len(arr) - 2):
        if arr[i] < arr[i-1] and arr[i] < arr[i-2] and arr[i] < arr[i+1] and arr[i] < arr[i+2]:
            levels.append(float(arr[i]))
    return levels


# ── Regime detection ──────────────────────────────────────────────────────────

def get_market_regime(vix: float, spy_daily_close: pd.Series) -> MarketRegime:
    """Classify BULL / BEAR / CHOP from SPY vs 50D+200D SMA and VIX."""
    if len(spy_daily_close) < 202:
        return MarketRegime.CHOP
    sma50  = float(_sma(spy_daily_close, 50).iloc[-1])
    sma200 = float(_sma(spy_daily_close, 200).iloc[-1])
    price  = float(spy_daily_close.iloc[-1])
    if price > sma50 and price > sma200 and vix < 20:
        return MarketRegime.BULL
    if price < sma50 and price < sma200 and vix > 25:
        return MarketRegime.BEAR
    return MarketRegime.CHOP


# ── Entry evaluation ──────────────────────────────────────────────────────────

def evaluate_entry(
    symbol:        str,
    hourly:        pd.DataFrame,   # OHLCV, 1H bars
    daily:         pd.DataFrame,   # OHLCV, daily bars
    vix:           float,
    ivr:           float,
    regime:        MarketRegime,
    account_value: float = 1000.0,
    is_fomc_cpi:   bool  = False,
) -> VortexSignal:
    """
    Evaluate all 5 VORTEX-1 entry filters.
    Returns VortexSignal with action='BUY_LONG' only when all 5 pass.
    """
    passed: List[str] = []
    failed: List[str] = []

    min_h = EMA_1H + RSI_PERIOD + 5
    min_d = SMA_DAILY + 5
    if len(hourly) < min_h or len(daily) < min_d:
        return VortexSignal(symbol=symbol, action="PASS", regime=regime.value,
                            score=0, filters_passed=[],
                            filters_failed=["INSUFFICIENT_DATA"])

    close_h = hourly["Close"]
    open_h  = hourly["Open"]
    high_h  = hourly["High"]
    low_h   = hourly["Low"]
    vol_h   = hourly["Volume"]
    close_d = daily["Close"]

    price = float(close_h.iloc[-1])

    # ── Filter 1: Trend alignment
    ema200  = float(_ema(close_h, EMA_1H).iloc[-1])
    sma50d  = float(_sma(close_d, SMA_DAILY).iloc[-1])
    daily_p = float(close_d.iloc[-1])
    f1_pass = price > ema200 and daily_p > sma50d
    if f1_pass:
        passed.append(f"TREND_ALIGNMENT (price={price:.2f} > EMA200={ema200:.2f})")
    else:
        reasons = []
        if price <= ema200:
            reasons.append(f"price {price:.2f} <= EMA200 {ema200:.2f}")
        if daily_p <= sma50d:
            reasons.append(f"daily {daily_p:.2f} <= SMA50 {sma50d:.2f}")
        failed.append(f"TREND_ALIGNMENT ({'; '.join(reasons)})")

    # ── Filter 2: Momentum exhaustion — RSI dipped < 35 in last 3 bars, now rising
    rsi_s    = _rsi(close_h, RSI_PERIOD)
    rsi_now  = float(rsi_s.iloc[-1])
    rsi_prev = float(rsi_s.iloc[-2])
    rsi_win  = rsi_s.iloc[-1 - RSI_LOOKBACK: -1]
    dipped   = bool((rsi_win < RSI_OVERSOLD).any())
    rising   = rsi_now > rsi_prev
    if dipped and rising:
        passed.append(f"MOMENTUM_EXHAUSTION (RSI={rsi_now:.1f}, was {rsi_win.min():.1f})")
    else:
        reasons = []
        if not dipped:
            reasons.append(f"RSI min={rsi_win.min():.1f} never below {RSI_OVERSOLD}")
        if not rising:
            reasons.append(f"RSI not rising ({rsi_prev:.1f}→{rsi_now:.1f})")
        failed.append(f"MOMENTUM_EXHAUSTION ({'; '.join(reasons)})")

    # ── Filter 3: Volume confirmation
    avg_vol   = float(vol_h.iloc[-VOL_AVG_BARS - 1: -1].mean())
    curr_vol  = float(vol_h.iloc[-1])
    vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0.0
    if vol_ratio >= VOL_MULT:
        passed.append(f"VOLUME_CONFIRM ({vol_ratio:.2f}x)")
    else:
        failed.append(f"VOLUME_CONFIRM ({vol_ratio:.2f}x < {VOL_MULT}x)")

    # ── Filter 4: Volatility window
    if is_fomc_cpi:
        failed.append("FOMC_CPI_BLACKOUT")
    elif VIX_MIN <= vix <= VIX_MAX and IVR_MIN <= ivr <= IVR_MAX:
        passed.append(f"VOLATILITY_WINDOW (VIX={vix:.1f}, IVR={ivr:.0f}%)")
    else:
        reasons = []
        if not (VIX_MIN <= vix <= VIX_MAX):
            reasons.append(f"VIX={vix:.1f} outside [{VIX_MIN},{VIX_MAX}]")
        if not (IVR_MIN <= ivr <= IVR_MAX):
            reasons.append(f"IVR={ivr:.0f}% outside [{IVR_MIN},{IVR_MAX}]")
        failed.append(f"VOLATILITY_WINDOW ({'; '.join(reasons)})")

    # ── Filter 5: Price structure
    supports   = _find_supports(low_h, lookback=60)
    at_support = any(abs(price - s) / s <= SUPPORT_TOL for s in supports) if supports else False
    hammer     = _is_hammer(float(open_h.iloc[-1]), float(high_h.iloc[-1]),
                             float(low_h.iloc[-1]), float(close_h.iloc[-1]))
    engulf     = (len(open_h) >= 2 and
                  _is_bullish_engulfing(float(open_h.iloc[-2]), float(close_h.iloc[-2]),
                                        float(open_h.iloc[-1]), float(close_h.iloc[-1])))
    if at_support or hammer or engulf:
        tag = "support" if at_support else ("hammer" if hammer else "engulfing")
        passed.append(f"PRICE_STRUCTURE ({tag})")
    else:
        failed.append("PRICE_STRUCTURE (no support/hammer/engulfing found)")

    score  = len(passed)
    action = "PASS"
    entry_price = stop_price = tp1_price = tp2_price = None
    r_dollar = shares = None

    if score == 5:
        tp2_r = (TP2_R_BULL if regime == MarketRegime.BULL else
                 TP2_R_BEAR if regime == MarketRegime.BEAR else TP2_R_NORMAL)

        entry_price = price
        stop_price  = float(low_h.iloc[-1])          # below signal candle low
        r_dist      = entry_price - stop_price        # price distance = 1R
        if r_dist > 0:
            r_dollar    = account_value * RISK_PCT / r_dist     # shares such that 1 stop-out = 2%
            tp1_price   = round(entry_price + TP1_R * r_dist, 4)
            tp2_price   = round(entry_price + tp2_r  * r_dist, 4)
            max_shares  = int((account_value * MAX_STOCK_PCT) / entry_price)
            shares      = int(min(r_dollar, max_shares))
            if shares >= 1:
                action = "BUY_LONG"

    return VortexSignal(
        symbol         = symbol,
        action         = action,
        regime         = regime.value,
        score          = score,
        filters_passed = passed,
        filters_failed = failed,
        entry_price    = entry_price,
        stop_price     = stop_price,
        tp1_price      = tp1_price,
        tp2_price      = tp2_price,
        r_dollar       = r_dollar,
        shares         = shares,
        current_rsi    = float(rsi_s.iloc[-1]),
        current_volume = curr_vol,
        avg_volume     = avg_vol,
        vix            = vix,
        ivr            = ivr,
    )


# ── Exit evaluation ───────────────────────────────────────────────────────────

def evaluate_exit(
    position:      VortexPosition,
    current_price: float,
    current_time:  datetime,
    candle_low:    float,
) -> VortexExitSignal:
    """
    Evaluate VORTEX-1 exit conditions in priority order.
    Call on every new 1H bar close while position is open.
    """
    sym  = position.symbol
    hhmm = current_time.hour * 100 + current_time.minute

    # ── Time stop: 3:45 PM
    if hhmm >= TIME_STOP_HM:
        return VortexExitSignal(sym, "CLOSE_ALL", "TIME_STOP_3:45PM", "EOD", current_price)

    # ── Determine effective stop
    effective_stop = position.entry_price if position.stop_be else position.stop_price
    if position.trail_active:
        effective_stop = max(effective_stop, position.trail_low)

    # ── Hard / trailing / breakeven stop
    if current_price <= effective_stop:
        reason = ("TRAILING_STOP"   if position.trail_active else
                  "BREAKEVEN_STOP"  if position.stop_be else
                  "HARD_STOP_LOSS")
        return VortexExitSignal(sym, "CLOSE_ALL", reason, "IMMEDIATE", current_price)

    # ── TP1: +1.5R → close half, move stop to breakeven
    if not position.half_closed and current_price >= position.tp1_price:
        return VortexExitSignal(sym, "CLOSE_HALF", "TP1_+1.5R", "IMMEDIATE", current_price)

    # ── TP2: full close
    if position.half_closed and current_price >= position.tp2_price:
        return VortexExitSignal(sym, "CLOSE_ALL", "TP2_+2.5R", "IMMEDIATE", current_price)

    return VortexExitSignal(sym, "HOLD", "NO_TRIGGER", "NORMAL", current_price)


# ── Position sizing ───────────────────────────────────────────────────────────

def calc_vortex_size(
    account_value: float,
    entry_price:   float,
    stop_price:    float,
    regime:        MarketRegime = MarketRegime.CHOP,
) -> dict:
    """
    Calculate VORTEX-1 position size.
    Returns shares, dollar_risk, dollar_size, r_multiple info.
    """
    r_dist = entry_price - stop_price
    if r_dist <= 0 or entry_price <= 0:
        return {"error": "stop must be below entry price"}

    base_risk_pct = RISK_PCT
    if regime == MarketRegime.BEAR:
        base_risk_pct *= 0.5   # halve size in bear regime

    dollar_risk   = account_value * base_risk_pct
    raw_shares    = dollar_risk / r_dist
    max_shares    = (account_value * MAX_STOCK_PCT) / entry_price
    shares        = int(min(raw_shares, max_shares))
    dollar_size   = shares * entry_price
    pct_of_acct   = dollar_size / account_value * 100

    tp2_r = (TP2_R_BULL if regime == MarketRegime.BULL else
             TP2_R_BEAR if regime == MarketRegime.BEAR else TP2_R_NORMAL)

    return {
        "shares":            shares,
        "entry_price":       entry_price,
        "stop_price":        stop_price,
        "dollar_risk":       round(dollar_risk, 2),
        "dollar_size":       round(dollar_size, 2),
        "pct_of_account":    round(pct_of_acct, 2),
        "r_distance":        round(r_dist, 4),
        "tp1_price":         round(entry_price + TP1_R   * r_dist, 4),
        "tp2_price":         round(entry_price + tp2_r   * r_dist, 4),
        "tp1_gain_pct":      round(TP1_R   * r_dist / entry_price * 100, 2),
        "tp2_gain_pct":      round(tp2_r   * r_dist / entry_price * 100, 2),
        "stop_loss_pct":     round(r_dist  / entry_price * 100, 2),
        "regime":            regime.value,
        "risk_pct_used":     round(base_risk_pct * 100, 1),
    }


# ── Universe scanner ──────────────────────────────────────────────────────────

def scan_vortex_universe(account_value: float = 1000.0) -> dict:
    """
    Scan the full VORTEX-1 tradeable universe via yfinance.
    Fetches 1H and daily OHLCV + ^VIX + SPY regime.
    Returns {regime, vix, signals[], timestamp}.
    """
    try:
        import yfinance as yf
    except ImportError:
        return {"error": "yfinance not available", "signals": []}

    # VIX
    try:
        vix = float(yf.Ticker(VIX_SYMBOL).fast_info.last_price)
    except Exception:
        vix = 22.0

    # SPY regime
    try:
        spy_d  = yf.Ticker(SPY_SYMBOL).history(period="300d", interval="1d")
        regime = get_market_regime(vix, spy_d["Close"])
    except Exception:
        regime = MarketRegime.CHOP

    signals: List[dict] = []
    for sym in TRADEABLE_UNIVERSE:
        try:
            t      = yf.Ticker(sym)
            hourly = t.history(period="60d",  interval="1h")
            daily  = t.history(period="300d", interval="1d")
            if hourly.empty or daily.empty:
                continue
            ivr = _hv_rank(daily["Close"])
            sig = evaluate_entry(sym, hourly, daily, vix, ivr, regime, account_value)
            signals.append(sig.to_dict())
        except Exception as exc:
            logger.warning("VORTEX scan %s: %s", sym, exc)

    signals.sort(key=lambda s: (0 if s["action"] == "BUY_LONG" else 1, -s["score"]))
    return {
        "regime":    regime.value,
        "vix":       vix,
        "signals":   signals,
        "universe":  TRADEABLE_UNIVERSE,
        "timestamp": int(time.time()),
    }
