"""
Nasdaq Data Link Adapter
=========================
Wraps the `nasdaqdatalink` library (formerly Quandl) for supplemental stock
analysis data that yfinance doesn't cover well:

  • FINRA short interest bi-weekly snapshots  — powers the squeeze scanner
  • EOD/WIKI historical OHLCV                  — high-quality price history
  • Macro time-series (FRED-equivalent)        — inflation, rates, VIX

Graceful degradation:
  - If NASDAQ_DATA_LINK_API_KEY is not set the adapter returns empty results
    (the rest of the platform continues to work using yfinance data).
  - Set the key in .env:  NASDAQ_DATA_LINK_API_KEY=your_key_here

Free-tier datasets that work without a subscription:
  WIKI/<TICKER>           historical OHLCV (legacy, still served)
  FRED/<CODE>             macro indicators (requires free key)

Premium datasets (need paid subscription):
  FINRA/FNYX_<TICKER>     NYSE short interest
  FINRA/FNSQ_<TICKER>     Nasdaq short interest
  ZACKS/FC                fundamental data
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Lazy import: gracefully handle missing package ────────────────────────────

try:
    import nasdaqdatalink as ndl
    _NDL_AVAILABLE = True
except ImportError:
    _NDL_AVAILABLE = False
    logger.info("nasdaqdatalink not installed; Nasdaq Data Link features disabled")

from config import NASDAQ_DATA_LINK_API_KEY  # set via env


def _configure():
    """Configure API key once on first use."""
    if not _NDL_AVAILABLE:
        return False
    if not NASDAQ_DATA_LINK_API_KEY:
        return False
    ndl.ApiConfig.api_key = NASDAQ_DATA_LINK_API_KEY
    return True


# ── Short Interest ─────────────────────────────────────────────────────────────

# FINRA reports short interest twice a month for NYSE and Nasdaq stocks.
# Dataset codes: FINRA/FNYX_<TICKER> (NYSE)  FINRA/FNSQ_<TICKER> (Nasdaq)
_FINRA_EXCHANGES = ["FNSQ", "FNYX"]  # try Nasdaq first, then NYSE


async def get_short_interest(ticker: str, days: int = 30) -> Optional[Dict]:
    """
    Fetch latest FINRA short interest data for a ticker.

    Returns:
        {
          "ticker": str,
          "short_interest": int,          # number of shares short
          "short_pct_float": float,       # short % of float (if available)
          "days_to_cover": float,         # short interest / avg daily volume
          "settlement_date": str,         # most recent settlement date
          "source": "FINRA"
        }
        or None if unavailable.
    """
    if not _configure():
        return None

    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days)

    def _fetch() -> Optional[Dict]:
        for exchange in _FINRA_EXCHANGES:
            code = f"FINRA/{exchange}_{ticker}"
            try:
                df = ndl.get(
                    code,
                    start_date=str(start_date),
                    end_date=str(end_date),
                    rows=5,
                )
                if df is None or df.empty:
                    continue
                latest = df.iloc[-1]
                row = {
                    "ticker": ticker,
                    "short_interest": int(latest.get("ShortVolume", latest.iloc[0])),
                    "settlement_date": str(df.index[-1].date()),
                    "source": "FINRA",
                    "exchange": exchange,
                }
                # Some FINRA codes include additional columns
                if "ShortExemptVolume" in df.columns:
                    row["short_exempt_volume"] = int(latest["ShortExemptVolume"])
                if "TotalVolume" in df.columns and latest["TotalVolume"] > 0:
                    row["short_pct_volume"] = round(
                        row["short_interest"] / latest["TotalVolume"], 4
                    )
                return row
            except Exception as e:
                logger.debug("FINRA %s %s: %s", exchange, ticker, e)
                continue
        return None

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _fetch)
    except Exception as e:
        logger.debug("get_short_interest(%s): %s", ticker, e)
        return None


async def get_short_interest_bulk(
    tickers: List[str], days: int = 30
) -> Dict[str, Optional[Dict]]:
    """Fetch short interest for multiple tickers concurrently."""
    tasks = {t: get_short_interest(t, days) for t in tickers}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    out: Dict[str, Optional[Dict]] = {}
    for ticker, result in zip(tasks.keys(), results):
        out[ticker] = None if isinstance(result, Exception) else result
    return out


# ── Historical OHLCV ──────────────────────────────────────────────────────────

async def get_ohlcv_history(
    ticker: str,
    start_date: str,
    end_date: Optional[str] = None,
    source: str = "WIKI",
) -> Optional[List[Dict]]:
    """
    Fetch high-quality historical OHLCV from Nasdaq Data Link.

    source: "WIKI" (free legacy), "EOD" (premium end-of-day)
    Returns list of {date, open, high, low, close, volume, adj_close}
    """
    if not _configure():
        return None

    if not end_date:
        end_date = str(datetime.utcnow().date())

    code = f"{source}/{ticker}"

    def _fetch() -> Optional[List[Dict]]:
        try:
            df = ndl.get(
                code,
                start_date=start_date,
                end_date=end_date,
            )
            if df is None or df.empty:
                return None
            df.index = df.index.astype(str)
            records = []
            for date, row in df.iterrows():
                record: Dict = {"date": date}
                # Normalise column names across WIKI vs EOD schemas
                col_map = {
                    "Open": "open", "High": "high", "Low": "low",
                    "Close": "close", "Volume": "volume",
                    "Adj. Close": "adj_close", "Adj. Open": "adj_open",
                    "Adj. High": "adj_high", "Adj. Low": "adj_low",
                    "Adj. Volume": "adj_volume",
                }
                for src_col, dst_col in col_map.items():
                    if src_col in row.index:
                        val = row[src_col]
                        record[dst_col] = float(val) if val is not None else None
                records.append(record)
            return records
        except Exception as e:
            logger.debug("get_ohlcv_history(%s, %s): %s", code, start_date, e)
            return None

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _fetch)
    except Exception as e:
        logger.debug("get_ohlcv_history executor: %s", e)
        return None


# ── Macro time-series ─────────────────────────────────────────────────────────

# Common FRED dataset codes available via Nasdaq Data Link
FRED_CODES = {
    "fed_funds_rate":   "FRED/FEDFUNDS",
    "cpi_yoy":          "FRED/CPIAUCSL",
    "unemployment":     "FRED/UNRATE",
    "gdp_growth":       "FRED/A191RL1Q225SBEA",
    "10y_treasury":     "FRED/DGS10",
    "2y_treasury":      "FRED/DGS2",
    "vix":              "CBOE/VIX",
    "sp500":            "MULTPL/SP500_PE_RATIO_MONTH",
}


async def get_macro_series(
    series_key: str,
    rows: int = 12,
) -> Optional[List[Dict]]:
    """
    Fetch a macro time-series by friendly key name.

    series_key: one of FRED_CODES keys (e.g. "fed_funds_rate")
    Returns list of {date, value}
    """
    if not _configure():
        return None

    code = FRED_CODES.get(series_key)
    if not code:
        logger.warning("Unknown macro series: %s", series_key)
        return None

    def _fetch() -> Optional[List[Dict]]:
        try:
            df = ndl.get(code, rows=rows)
            if df is None or df.empty:
                return None
            df.index = df.index.astype(str)
            col = df.columns[0]
            return [
                {"date": str(date), "value": float(row[col])}
                for date, row in df.iterrows()
            ]
        except Exception as e:
            logger.debug("get_macro_series(%s): %s", code, e)
            return None

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _fetch)
    except Exception as e:
        logger.debug("get_macro_series executor: %s", e)
        return None


async def get_macro_snapshot() -> Dict[str, Optional[float]]:
    """
    Fetch the latest value of each major macro indicator.

    Returns { "fed_funds_rate": 5.33, "cpi_yoy": 3.1, ... }
    """
    tasks = {key: get_macro_series(key, rows=1) for key in FRED_CODES}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    snapshot: Dict[str, Optional[float]] = {}
    for key, result in zip(tasks.keys(), results):
        if isinstance(result, Exception) or not result:
            snapshot[key] = None
        else:
            try:
                snapshot[key] = result[-1]["value"]
            except (IndexError, KeyError, TypeError):
                snapshot[key] = None
    return snapshot


# ── Squeeze-enhanced short interest ──────────────────────────────────────────

async def enrich_with_short_interest(
    candidates: List[Dict],
    ticker_field: str = "ticker",
) -> List[Dict]:
    """
    Enrich a list of momentum candidates with FINRA short interest data.

    Each candidate dict gets a "nasdaq_short_interest" key added if data
    is available. The existing yfinance short_interest_pct is preserved as
    a fallback.

    Args:
        candidates: list of dicts with at least a ticker field
        ticker_field: key name for the ticker in each dict
    """
    if not _configure():
        return candidates  # no-op without API key

    tickers = [c[ticker_field] for c in candidates if ticker_field in c]
    if not tickers:
        return candidates

    si_data = await get_short_interest_bulk(tickers)

    for candidate in candidates:
        ticker = candidate.get(ticker_field)
        if not ticker:
            continue
        si = si_data.get(ticker)
        if si:
            candidate["nasdaq_short_interest"] = si
            # Override yfinance estimate with FINRA official figure if we have pct
            if "short_pct_volume" in si:
                candidate["short_interest_pct_finra"] = round(
                    si["short_pct_volume"] * 100, 2
                )
    return candidates


# ── Status check ──────────────────────────────────────────────────────────────

def get_status() -> Dict:
    """Return current Nasdaq Data Link adapter status."""
    return {
        "available":    _NDL_AVAILABLE,
        "key_set":      bool(NASDAQ_DATA_LINK_API_KEY),
        "fred_codes":   list(FRED_CODES.keys()),
        "note":         (
            "Set NASDAQ_DATA_LINK_API_KEY in .env to enable short interest, "
            "EOD prices, and FRED macro data."
            if not NASDAQ_DATA_LINK_API_KEY
            else "Nasdaq Data Link active"
        ),
    }
