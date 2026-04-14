"""
Multi-Timeframe Technical Analysis Engine
==========================================
Fetches three timeframes of OHLCV data and stacks:
  • Moving averages  — SMA 20/50/200, EMA 9/21 (daily); EMA 9/21/50 (hourly); EMA 9 (minute)
  • Volume trend     — OBV, Volume SMA 20, relative volume, volume/price divergence
  • Support / Resistance — swing highs/lows, pivot points (daily), VWAP (intraday)
  • ATR (14-period)  — volatility-normalised price distance

Timeframes:
  DAILY  : 1 year,  1d interval  (~252 bars)
  HOURLY : 3 months, 1h interval (~504 bars)
  MINUTE : 24 hours, 1m interval (~390 bars stocks / 1440 bars crypto)

Confluence scoring: each timeframe contributes ±1 per signal; final [-6, +6].
  ≥+4  → strong long opportunity
  +2–3 → moderate long
  ≤-4  → strong short opportunity
  -3–1 → neutral / wait

Works for both equities (SPY, NVDA, AAPL…) and crypto (BTC-USD, ETH-USD…).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=6)

# ── Timeframe specs ────────────────────────────────────────────────────────────

TIMEFRAMES = {
    "daily":  {"period": "1y",  "interval": "1d",  "label": "1Y Daily"},
    "hourly": {"period": "3mo", "interval": "1h",  "label": "3M Hourly"},
    "minute": {"period": "1d",  "interval": "1m",  "label": "24H Minute"},
}

# ── Low-level helpers ─────────────────────────────────────────────────────────

def _s(val) -> float:
    """Safe float cast."""
    try:
        f = float(val)
        return 0.0 if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return 0.0


def _fetch_ohlcv(ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    """Synchronous yfinance download. Run in executor."""
    try:
        df = yf.download(
            ticker, period=period, interval=interval,
            auto_adjust=True, progress=False,
        )
        if df is None or df.empty or len(df) < 5:
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        return df
    except Exception as exc:
        logger.warning("OHLCV fetch %s %s/%s: %s", ticker, period, interval, exc)
        return None


# ── Indicator calculations ─────────────────────────────────────────────────────

def _sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=max(1, n // 2)).mean()


def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """True Range → ATR."""
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def _obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    direction = df["Close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (direction * df["Volume"]).cumsum()


def _vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP — meaningful only on intraday data."""
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    cumvol  = df["Volume"].cumsum().replace(0, np.nan)
    return (typical * df["Volume"]).cumsum() / cumvol


def _swing_levels(df: pd.DataFrame, window: int = 20) -> Tuple[List[float], List[float]]:
    """
    Detect swing highs and lows using rolling extremes.
    Returns (resistance_prices, support_prices) sorted descending/ascending.
    """
    hi = df["High"].rolling(window, center=True, min_periods=window // 2).max()
    lo = df["Low"].rolling(window, center=True, min_periods=window // 2).min()

    resistances, supports = [], []
    price = _s(df["Close"].iloc[-1])

    # Find local peaks/troughs
    for i in range(window, len(df) - window // 2):
        h = _s(df["High"].iloc[i])
        l = _s(df["Low"].iloc[i])
        if h == _s(hi.iloc[i]) and h > price:
            resistances.append(round(h, 4))
        if l == _s(lo.iloc[i]) and l < price:
            supports.append(round(l, 4))

    # Cluster nearby levels (within 0.5%)
    def _cluster(lvls: list, tol: float = 0.005) -> list:
        if not lvls:
            return []
        lvls = sorted(set(lvls))
        out, group = [], [lvls[0]]
        for v in lvls[1:]:
            if v - group[-1] <= group[-1] * tol:
                group.append(v)
            else:
                out.append(round(sum(group) / len(group), 4))
                group = [v]
        out.append(round(sum(group) / len(group), 4))
        return out

    resistances = sorted(_cluster(resistances), reverse=True)[:5]
    supports    = sorted(_cluster(supports))[:5]
    return resistances, supports


def _pivot_points(df: pd.DataFrame) -> Dict:
    """Classic daily pivot points from the last completed bar."""
    if len(df) < 2:
        return {}
    last = df.iloc[-2]  # last completed candle
    H, L, C = _s(last["High"]), _s(last["Low"]), _s(last["Close"])
    P  = (H + L + C) / 3
    S1 = 2 * P - H
    S2 = P - (H - L)
    S3 = L - 2 * (H - P)
    R1 = 2 * P - L
    R2 = P + (H - L)
    R3 = H + 2 * (P - L)
    return {
        "P": round(P, 4), "R1": round(R1, 4), "R2": round(R2, 4), "R3": round(R3, 4),
        "S1": round(S1, 4), "S2": round(S2, 4), "S3": round(S3, 4),
    }


def _obv_trend(obv_series: pd.Series, lookback: int = 20) -> str:
    """Classify OBV slope as rising / falling / flat."""
    if len(obv_series) < lookback:
        return "flat"
    recent = obv_series.iloc[-lookback:]
    slope  = np.polyfit(range(len(recent)), recent.values, 1)[0]
    pct    = abs(slope) / (abs(obv_series.iloc[-1]) + 1e-9) * 100
    if pct < 0.01:
        return "flat"
    return "rising" if slope > 0 else "falling"


def _vol_trend(df: pd.DataFrame, window: int = 20) -> str:
    """Volume trend vs its own rolling average."""
    vol_sma = _s(df["Volume"].rolling(window, min_periods=5).mean().iloc[-1])
    cur_vol  = _s(df["Volume"].iloc[-1])
    if vol_sma == 0:
        return "flat"
    ratio = cur_vol / vol_sma
    if ratio >= 1.5:
        return "surging"
    if ratio >= 1.1:
        return "rising"
    if ratio <= 0.6:
        return "drying_up"
    return "flat"


# ── Per-timeframe analysis ────────────────────────────────────────────────────

def _analyse_timeframe(df: pd.DataFrame, label: str, is_intraday: bool = False) -> Dict:
    """
    Compute all indicators for one timeframe DataFrame.
    Returns a dict with indicator values and a directional score.
    """
    price = _s(df["Close"].iloc[-1])
    if price == 0:
        return {"error": "zero price", "score": 0}

    # ── Moving averages ───────────────────────────────────────────────────────
    ema9  = _s(_ema(df["Close"], 9).iloc[-1])
    ema21 = _s(_ema(df["Close"], 21).iloc[-1])
    sma20 = _s(_sma(df["Close"], 20).iloc[-1])
    sma50 = _s(_sma(df["Close"], 50).iloc[-1])
    sma200 = _s(_sma(df["Close"], 200).iloc[-1]) if len(df) >= 100 else 0.0

    ma_score = 0
    ma_signals = []

    if sma20 and price > sma20:
        ma_score += 1; ma_signals.append("P>SMA20")
    elif sma20:
        ma_score -= 1; ma_signals.append("P<SMA20")

    if sma50 and price > sma50:
        ma_score += 1; ma_signals.append("P>SMA50")
    elif sma50:
        ma_score -= 1; ma_signals.append("P<SMA50")

    if sma200 and price > sma200:
        ma_score += 1; ma_signals.append("P>SMA200")
    elif sma200:
        ma_score -= 1; ma_signals.append("P<SMA200")

    if ema9 and ema21 and ema9 > ema21:
        ma_score += 1; ma_signals.append("EMA9>EMA21")
    elif ema9 and ema21:
        ma_score -= 1; ma_signals.append("EMA9<EMA21")

    ma_trend = "bullish" if ma_score >= 2 else "bearish" if ma_score <= -2 else "mixed"

    # ── ATR ───────────────────────────────────────────────────────────────────
    atr_series = _atr(df)
    atr14 = _s(atr_series.iloc[-1])
    atr_pct = (atr14 / price * 100) if price else 0.0

    # ── Volume / OBV ─────────────────────────────────────────────────────────
    obv_series    = _obv(df)
    obv_now       = _s(obv_series.iloc[-1])
    obv_tr        = _obv_trend(obv_series)
    vol_tr        = _vol_trend(df)
    vol_sma20     = _s(df["Volume"].rolling(20, min_periods=5).mean().iloc[-1])
    rvol          = _s(df["Volume"].iloc[-1]) / (vol_sma20 or 1)

    vol_score = 0
    if obv_tr == "rising":
        vol_score += 1
    elif obv_tr == "falling":
        vol_score -= 1

    if vol_tr in ("surging", "rising") and ma_score >= 0:
        vol_score += 1  # rising volume on bullish MA → confirms
    elif vol_tr in ("surging", "rising") and ma_score < 0:
        vol_score -= 1  # rising volume on bearish MA → pressure

    # ── Support / Resistance ──────────────────────────────────────────────────
    win = 15 if is_intraday else 20
    resistance, support = _swing_levels(df, window=win)
    pivots = _pivot_points(df) if not is_intraday else {}

    nearest_r = resistance[0] if resistance else price * 1.05
    nearest_s = support[0]    if support    else price * 0.95
    dist_to_r = (nearest_r - price) / price if nearest_r else 0.05
    dist_to_s = (price - nearest_s) / price if nearest_s else 0.05

    sr_score = 0
    if dist_to_s <= 0.01 and dist_to_s >= 0:        # price sitting on support
        sr_score += 1
    if dist_to_r <= 0.01 and dist_to_r >= 0:        # price at resistance
        sr_score -= 1

    # ── VWAP (intraday only) ──────────────────────────────────────────────────
    vwap_val = None
    vwap_score = 0
    if is_intraday and len(df) >= 10:
        vwap_val = _s(_vwap(df).iloc[-1])
        if vwap_val:
            if price > vwap_val:
                vwap_score = 1
            else:
                vwap_score = -1

    # ── Candle data for frontend charting (last N bars) ──────────────────────
    candle_count = 100 if is_intraday else 252
    candles = []
    for idx, row in df.tail(candle_count).iterrows():
        ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(time.time())
        candles.append({
            "time":   ts,
            "open":   round(_s(row["Open"]), 4),
            "high":   round(_s(row["High"]), 4),
            "low":    round(_s(row["Low"]), 4),
            "close":  round(_s(row["Close"]), 4),
            "volume": int(_s(row["Volume"])),
        })

    # ── MA values for overlay ────────────────────────────────────────────────
    ma_overlay = {}
    for n, series in [(9, _ema(df["Close"], 9)),
                      (21, _ema(df["Close"], 21)),
                      (20, _sma(df["Close"], 20)),
                      (50, _sma(df["Close"], 50)),
                      (200, _sma(df["Close"], 200))]:
        tail = series.tail(candle_count)
        ma_overlay[f"{'ema' if n in (9, 21) else 'sma'}{n}"] = [
            {"time": int(i.timestamp()), "value": round(_s(v), 4)}
            for i, v in zip(tail.index, tail.values)
            if _s(v) > 0
        ]

    if is_intraday and vwap_val:
        vwap_series = _vwap(df).tail(candle_count)
        ma_overlay["vwap"] = [
            {"time": int(i.timestamp()), "value": round(_s(v), 4)}
            for i, v in zip(vwap_series.index, vwap_series.values)
            if _s(v) > 0
        ]

    # ── Volume data ──────────────────────────────────────────────────────────
    vol_data = [
        {"time": int(idx.timestamp()), "value": int(_s(row["Volume"])),
         "color": "rgba(34,197,94,0.5)" if _s(row["Close"]) >= _s(row["Open"])
                  else "rgba(239,68,68,0.5)"}
        for idx, row in df.tail(candle_count).iterrows()
    ]

    total_score = ma_score + vol_score + sr_score + vwap_score

    return {
        "label":       label,
        "bars":        len(df),
        "price":       round(price, 4),
        # MAs
        "ema9":        round(ema9, 4),
        "ema21":       round(ema21, 4),
        "sma20":       round(sma20, 4),
        "sma50":       round(sma50, 4),
        "sma200":      round(sma200, 4),
        "ma_trend":    ma_trend,
        "ma_score":    ma_score,
        "ma_signals":  ma_signals,
        # ATR
        "atr14":       round(atr14, 4),
        "atr_pct":     round(atr_pct, 3),
        # Volume
        "obv":         round(obv_now, 0),
        "obv_trend":   obv_tr,
        "vol_trend":   vol_tr,
        "rvol":        round(rvol, 2),
        "vol_score":   vol_score,
        # S/R
        "resistance":  resistance,
        "support":     support,
        "nearest_r":   round(nearest_r, 4),
        "nearest_s":   round(nearest_s, 4),
        "dist_to_r":   round(dist_to_r * 100, 2),    # %
        "dist_to_s":   round(dist_to_s * 100, 2),    # %
        "pivots":      pivots,
        "sr_score":    sr_score,
        # VWAP
        "vwap":        round(vwap_val, 4) if vwap_val else None,
        "vwap_score":  vwap_score,
        # Total
        "score":       total_score,
        # Chart data
        "candles":     candles,
        "ma_overlay":  ma_overlay,
        "volume":      vol_data,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

async def get_mtf_analysis(ticker: str) -> Dict:
    """
    Full multi-timeframe analysis for one ticker.
    Fetches daily/hourly/minute in parallel, stacks all indicators.
    """
    ticker = ticker.upper().strip()
    loop   = asyncio.get_event_loop()

    # Parallel fetches for all three timeframes
    daily_fut  = loop.run_in_executor(_executor, _fetch_ohlcv, ticker, "1y",  "1d")
    hourly_fut = loop.run_in_executor(_executor, _fetch_ohlcv, ticker, "3mo", "1h")
    minute_fut = loop.run_in_executor(_executor, _fetch_ohlcv, ticker, "1d",  "1m")

    daily_df, hourly_df, minute_df = await asyncio.gather(
        daily_fut, hourly_fut, minute_fut
    )

    errors = []
    if daily_df  is None: errors.append("daily")
    if hourly_df is None: errors.append("hourly")
    if minute_df is None: errors.append("minute")

    if len(errors) == 3:
        return {"error": f"No data for {ticker}", "ticker": ticker}

    # Run analysis in executor (numpy/pandas is CPU-bound)
    results = {}
    if daily_df is not None:
        results["daily"]  = await loop.run_in_executor(
            _executor, _analyse_timeframe, daily_df, "1Y Daily", False
        )
    if hourly_df is not None:
        results["hourly"] = await loop.run_in_executor(
            _executor, _analyse_timeframe, hourly_df, "3M Hourly", False
        )
    if minute_df is not None:
        results["minute"] = await loop.run_in_executor(
            _executor, _analyse_timeframe, minute_df, "24H Minute", True
        )

    # ── MTF confluence score ─────────────────────────────────────────────────
    confluence = sum(r.get("score", 0) for r in results.values())

    if confluence >= 4:
        direction = "STRONG_LONG"
    elif confluence >= 2:
        direction = "LONG"
    elif confluence <= -4:
        direction = "STRONG_SHORT"
    elif confluence <= -2:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    # Current price (from most liquid timeframe)
    price = 0.0
    for tf in ("daily", "hourly", "minute"):
        if tf in results:
            price = results[tf].get("price", 0.0)
            if price:
                break

    # ATR from daily (best for position sizing)
    daily_atr = results.get("daily", {}).get("atr14", 0)

    return {
        "ticker":            ticker,
        "price":             round(price, 4),
        "confluence_score":  confluence,
        "direction":         direction,
        "daily_atr":         round(daily_atr, 4),
        "timeframes":        results,
        "errors":            errors,
        "timestamp":         int(time.time()),
    }
