"""
FRED (Federal Reserve Economic Data) adapter.
Provides macro context for the news correlator.
Requires a free FRED API key (https://fred.stlouisfed.org/docs/api/api_key.html).
Degrades gracefully to empty when no key is set.
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional

from config import FRED_API_KEY, HTTP_TIMEOUT

logger = logging.getLogger(__name__)

# Key FRED series IDs we care about
SERIES = {
    "fed_funds_rate": "FEDFUNDS",
    "cpi_yoy": "CPIAUCSL",
    "unemployment": "UNRATE",
    "10y_treasury": "DGS10",
    "2y_treasury": "DGS2",
    "vix": "VIXCLS",
    "us_gdp_growth": "A191RL1Q225SBEA",
    "pce": "PCE",
}


def _fred_client():
    if not FRED_API_KEY:
        return None
    try:
        from fredapi import Fred
        return Fred(api_key=FRED_API_KEY)
    except ImportError:
        logger.warning("fredapi not installed")
        return None


def get_latest_macro_indicators() -> Dict[str, Optional[float]]:
    """
    Returns the latest value for each key macro indicator.
    Returns empty dict if no key is configured.
    """
    fred = _fred_client()
    if not fred:
        return {}
    results = {}
    for label, series_id in SERIES.items():
        try:
            s = fred.get_series(series_id, observation_start="2023-01-01")
            if s is not None and not s.empty:
                results[label] = round(float(s.dropna().iloc[-1]), 4)
            else:
                results[label] = None
        except Exception as e:
            logger.debug("FRED series %s: %s", series_id, e)
            results[label] = None
    return results


def get_yield_curve_spread() -> Optional[float]:
    """10Y-2Y spread — negative = inverted (recession signal)."""
    fred = _fred_client()
    if not fred:
        return None
    try:
        t10 = fred.get_series("DGS10", observation_start="2024-01-01")
        t2 = fred.get_series("DGS2", observation_start="2024-01-01")
        if t10 is not None and t2 is not None:
            return round(float(t10.dropna().iloc[-1]) - float(t2.dropna().iloc[-1]), 4)
    except Exception as e:
        logger.debug("yield curve: %s", e)
    return None
