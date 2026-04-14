"""
Implied Volatility & IV-Based Trade Setup
==========================================
Core principle: stop-loss and take-profit distances are sized relative to the
instrument's *expected daily move* (derived from IV), not a fixed dollar amount
or portfolio percentage. This means:

  • A 30 IV% stock gets a wider stop than a 15 IV% stock (both in $ terms).
  • A 90-IV-percentile stock uses a tighter R:R ratio (expensive premium,
    high mean-reversion risk if holding a directional position).
  • A 10-IV-percentile stock uses a wider R:R (cheap vol, hold for the move).

IV Sources:
  • Equities  — yfinance nearest ATM call's impliedVolatility field
  • Crypto    — 30-day historical volatility (options data less reliable/accessible)

Formulas:
  daily_expected_move   = price × IV_annual / √252
  weekly_expected_move  = price × IV_annual / √52
  stop_distance         = max(ATR × 1.5,  daily_EM × 0.75)
  R:R ratio             = f(IV_percentile):
                            IVP  0-25  → 3.0 : 1
                            IVP 25-50  → 2.5 : 1
                            IVP 50-75  → 2.0 : 1
                            IVP 75-90  → 1.75 : 1
                            IVP 90+    → 1.5 : 1  (avoid trading high-IV unless strong signal)
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=4)

CRYPTO_SUFFIXES = ("-USD", "-USDT", "-BTC", "-ETH")


def _is_crypto(ticker: str) -> bool:
    t = ticker.upper()
    return any(t.endswith(s) for s in CRYPTO_SUFFIXES)


def _s(val) -> float:
    try:
        f = float(val)
        return 0.0 if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return 0.0


# ── Historical volatility (always available) ───────────────────────────────────

def _calc_hv(close: pd.Series, period: int) -> float:
    """Annualised historical volatility from log returns."""
    if len(close) < period + 1:
        return 0.0
    lr = np.log(close / close.shift(1)).dropna()
    if len(lr) < period:
        return 0.0
    roll_std = lr.rolling(period).std().iloc[-1]
    return _s(roll_std * math.sqrt(252))


def _calc_iv_percentile(ticker: str, current_iv: float) -> float:
    """
    Rank current IV against 1-year rolling HV to produce a percentile 0-100.
    Uses 30-day HV sampled over the past year as the IV proxy history.
    """
    try:
        df = yf.download(ticker, period="1y", interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            return 50.0
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        close = df["Close"].dropna()
        lr    = np.log(close / close.shift(1)).dropna()
        hv30  = lr.rolling(30).std() * math.sqrt(252)
        hv30  = hv30.dropna()
        if hv30.empty:
            return 50.0
        rank = (hv30 < current_iv).sum() / len(hv30) * 100
        return round(float(rank), 1)
    except Exception as exc:
        logger.debug("IV percentile calc %s: %s", ticker, exc)
        return 50.0


# ── Implied volatility (options chain, equities only) ─────────────────────────

def _get_options_iv(ticker: str) -> Optional[float]:
    """
    Extract ATM implied volatility from the nearest front-month options chain.
    Returns annualised IV (e.g., 0.35 = 35 IV%), or None if unavailable.
    """
    try:
        t  = yf.Ticker(ticker)
        fi = t.fast_info
        price = _s(getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None))
        if price == 0:
            return None

        exps = t.options
        if not exps:
            return None

        # Use nearest expiry ≥ 7 days out
        now = time.time()
        target = None
        for exp in exps:
            try:
                exp_ts = pd.Timestamp(exp).timestamp()
            except Exception:
                continue
            if exp_ts - now >= 7 * 86400:
                target = exp
                break
        if not target:
            target = exps[0]

        chain = t.option_chain(target)
        calls = chain.calls.copy()
        if calls.empty:
            return None

        calls = calls[calls["impliedVolatility"] > 0]
        calls["dist"] = (calls["strike"] - price).abs()
        atm = calls.nsmallest(3, "dist")
        iv  = _s(atm["impliedVolatility"].mean())
        return iv if iv > 0.01 else None

    except Exception as exc:
        logger.debug("Options IV %s: %s", ticker, exc)
        return None


# ── ATR (14-period daily) ──────────────────────────────────────────────────────

def _get_daily_atr(ticker: str) -> float:
    """14-day ATR from 3-month daily data."""
    try:
        df = yf.download(ticker, period="3mo", interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            return 0.0
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        prev = df["Close"].shift(1)
        tr = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - prev).abs(),
            (df["Low"]  - prev).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(14, min_periods=7).mean()
        return _s(atr.iloc[-1])
    except Exception as exc:
        logger.debug("Daily ATR %s: %s", ticker, exc)
        return 0.0


# ── R:R ratio from IV percentile ─────────────────────────────────────────────

def _rr_from_ivp(ivp: float, confluence_score: int = 0) -> float:
    """
    Dynamic R:R ratio based on IV percentile.

    Low IVP  → premium is cheap → hold for a large move → higher R:R
    High IVP → premium expensive → mean-reversion risk → lower R:R

    Confluence score adjustments:
      Strong signal (+4 or -4): add 0.5 to R:R
      Weak signal  (+1 or -1): subtract 0.25
    """
    if ivp < 25:
        base = 3.0
    elif ivp < 50:
        base = 2.5
    elif ivp < 75:
        base = 2.0
    elif ivp < 90:
        base = 1.75
    else:
        base = 1.5

    # Bonus for high-confluence setups
    strength_adj = 0.0
    if abs(confluence_score) >= 4:
        strength_adj = 0.5
    elif abs(confluence_score) <= 1:
        strength_adj = -0.25

    return round(max(1.2, base + strength_adj), 2)


# ── Full volatility profile ────────────────────────────────────────────────────

def _build_volatility_profile(
    ticker: str,
    daily_df: Optional[pd.DataFrame] = None,
) -> Dict:
    """
    Build complete volatility profile:
      iv_annual, iv_percentile, hv_30, hv_252, atr14, daily_em, weekly_em
    """
    # Fetch daily data if not provided
    if daily_df is None or daily_df.empty:
        try:
            daily_df = yf.download(ticker, period="1y", interval="1d",
                                   auto_adjust=True, progress=False)
            if daily_df is not None:
                daily_df.columns = [c[0] if isinstance(c, tuple) else c
                                    for c in daily_df.columns]
        except Exception:
            daily_df = None

    close = daily_df["Close"].dropna() if daily_df is not None and not daily_df.empty else pd.Series()
    price = _s(close.iloc[-1]) if not close.empty else 0.0

    # Historical volatilities
    hv30  = _calc_hv(close, 30)
    hv252 = _calc_hv(close, 252)

    # ATR
    atr14 = 0.0
    if daily_df is not None and not daily_df.empty:
        prev = daily_df["Close"].shift(1)
        tr = pd.concat([
            daily_df["High"] - daily_df["Low"],
            (daily_df["High"] - prev).abs(),
            (daily_df["Low"]  - prev).abs(),
        ], axis=1).max(axis=1)
        atr14 = _s(tr.rolling(14, min_periods=7).mean().iloc[-1])

    # Implied volatility
    iv_source = "historical"
    iv_annual = hv30  # fallback

    if not _is_crypto(ticker):
        options_iv = _get_options_iv(ticker)
        if options_iv and options_iv > 0.01:
            iv_annual = options_iv
            iv_source = "options"

    if iv_annual == 0 and hv30 > 0:
        iv_annual = hv30

    # IV percentile (rank current IV vs trailing 1Y HV distribution)
    ivp = _calc_iv_percentile(ticker, iv_annual)

    # Expected moves
    daily_em  = (price * iv_annual / math.sqrt(252)) if iv_annual and price else 0.0
    weekly_em = (price * iv_annual / math.sqrt(52))  if iv_annual and price else 0.0

    atr_pct = (atr14 / price * 100) if price else 0.0

    return {
        "ticker":       ticker,
        "price":        round(price, 4),
        "iv_annual":    round(iv_annual * 100, 2),   # in %
        "iv_source":    iv_source,
        "iv_percentile": ivp,
        "hv_30":        round(hv30 * 100, 2),
        "hv_252":       round(hv252 * 100, 2),
        "atr_14":       round(atr14, 4),
        "atr_pct":      round(atr_pct, 3),
        "daily_em":     round(daily_em, 4),    # expected daily move in $
        "daily_em_pct": round((daily_em / price * 100) if price else 0, 3),
        "weekly_em":    round(weekly_em, 4),
        "weekly_em_pct":round((weekly_em / price * 100) if price else 0, 3),
    }


# ── Trade setup calculator ─────────────────────────────────────────────────────

def calc_trade_setup(
    ticker:           str,
    entry_price:      float,
    side:             str,           # "long" or "short"
    vol_profile:      Dict,          # from _build_volatility_profile
    confluence_score: int = 0,
) -> Dict:
    """
    Calculate entry / stop / target using IV-based sizing.

    Stop distance = max(ATR × 1.5,  daily_expected_move × 0.75)
    This ensures the stop survives normal daily noise for this instrument.

    Target distance = stop_distance × R:R_ratio

    R:R ratio is IV-percentile-driven (see _rr_from_ivp).
    """
    iv_annual  = vol_profile.get("iv_annual", 30.0) / 100   # back to decimal
    ivp        = vol_profile.get("iv_percentile", 50.0)
    atr14      = vol_profile.get("atr_14", 0.0)
    daily_em   = vol_profile.get("daily_em", 0.0)
    price      = entry_price or vol_profile.get("price", 0.0)

    if price == 0:
        return {"error": "Cannot calculate setup: price is 0"}

    # Stop distance: IV-normalised, never tighter than 1× ATR
    if daily_em > 0 and atr14 > 0:
        stop_dist = max(atr14 * 1.5, daily_em * 0.75)
    elif atr14 > 0:
        stop_dist = atr14 * 1.5
    elif daily_em > 0:
        stop_dist = daily_em * 0.75
    else:
        stop_dist = price * 0.02   # 2% fallback

    stop_pct   = stop_dist / price * 100
    rr_ratio   = _rr_from_ivp(ivp, confluence_score)
    target_dist = stop_dist * rr_ratio
    target_pct  = target_dist / price * 100

    if side.lower() == "long":
        stop   = price - stop_dist
        target = price + target_dist
    else:
        stop   = price + stop_dist
        target = price - target_dist

    stop   = round(max(0, stop), 4)
    target = round(max(0, target), 4)

    # Signal strength label
    if abs(confluence_score) >= 4:
        strength = "strong"
    elif abs(confluence_score) >= 2:
        strength = "moderate"
    else:
        strength = "weak"

    # Rationale string (displayed in UI)
    iv_lbl    = f"IV {vol_profile.get('iv_annual', 0):.0f}% ({vol_profile.get('iv_source', '?')})"
    ivp_lbl   = f"IVP {ivp:.0f}th"
    atr_lbl   = f"ATR ${atr14:.2f} ({vol_profile.get('atr_pct', 0):.1f}%)"
    em_lbl    = f"daily EM ${daily_em:.2f}"
    rationale = (
        f"{iv_lbl} | {ivp_lbl} → {rr_ratio}:1 R/R | "
        f"{atr_lbl} | {em_lbl} | stop ${stop_dist:.2f} ({stop_pct:.1f}%)"
    )

    return {
        "ticker":           ticker,
        "side":             side.lower(),
        "entry":            round(price, 4),
        "stop":             stop,
        "target":           target,
        "stop_distance":    round(stop_dist, 4),
        "stop_pct":         round(stop_pct, 2),
        "target_distance":  round(target_dist, 4),
        "target_pct":       round(target_pct, 2),
        "rr_ratio":         rr_ratio,
        "iv_percentile":    ivp,
        "daily_em":         round(daily_em, 4),
        "atr_14":           round(atr14, 4),
        "confluence_score": confluence_score,
        "signal_strength":  strength,
        "rationale":        rationale,
    }


# ── Public async entry points ─────────────────────────────────────────────────

async def get_volatility_profile(ticker: str) -> Dict:
    """Async wrapper for _build_volatility_profile."""
    ticker = ticker.upper().strip()
    loop   = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, _build_volatility_profile, ticker, None
    )


async def get_trade_setup(
    ticker:           str,
    side:             str,
    entry_price:      float  = 0.0,
    confluence_score: int    = 0,
) -> Dict:
    """
    Full pipeline: fetch volatility profile → calculate IV-based trade setup.

    side          : "long" or "short"
    entry_price   : 0 = use current market price
    confluence_score : from technical.get_mtf_analysis() (optional, refines R:R)
    """
    ticker = ticker.upper().strip()
    loop   = asyncio.get_event_loop()

    # Fetch vol profile
    vp = await loop.run_in_executor(
        _executor, _build_volatility_profile, ticker, None
    )
    if "error" in vp:
        return vp

    ep = entry_price or vp.get("price", 0.0)
    if ep == 0:
        return {"error": f"No price data for {ticker}"}

    setup = calc_trade_setup(ticker, ep, side, vp, confluence_score)
    return {
        **vp,
        "setup":          setup,
        "side":           side.lower(),
        "timestamp":      int(time.time()),
    }
