"""
Funda AI API Adapter
=====================
Python client for https://api.funda.ai/v1

Covers the endpoints most useful for equity research in this app:
  • Earnings calendar (upcoming + historical)
  • Key metrics / fundamentals (TTM P/E, ROE, margins, etc.)
  • Analyst estimates + price targets
  • Company profile
  • Options flow / GEX  (if key permits)
  • SEC filings (8-K, 10-Q, 10-K)
  • Congressional trades
  • Economic calendar (Fed, CPI, GDP events)

All calls degrade gracefully — returns None / [] if key is missing or API
returns an error, so the rest of the app keeps working without a key.

Auth: Authorization: Bearer <FUNDA_API_KEY>
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import FUNDA_API_KEY, HTTP_TIMEOUT

logger = logging.getLogger(__name__)

_BASE = "https://api.funda.ai/v1"


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {FUNDA_API_KEY}"}


def _get(path: str, params: Dict[str, Any] | None = None) -> Any:
    """Synchronous GET helper. Returns parsed JSON or None on failure."""
    if not FUNDA_API_KEY:
        return None
    url = f"{_BASE}{path}"
    try:
        r = httpx.get(url, headers=_headers(), params=params or {}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        logger.warning("Funda API %s → HTTP %d", url, e.response.status_code)
    except Exception as e:
        logger.debug("Funda API %s → %s", url, e)
    return None


# ── Earnings Calendar ──────────────────────────────────────────────────────────

def get_earnings_calendar(
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    ticker: Optional[str] = None,
    limit: int = 50,
) -> List[Dict]:
    """
    Upcoming earnings announcements.
    Returns list of dicts with keys: date, ticker, epsEstimated, revenueEstimated, time.
    """
    today = datetime.now(timezone.utc)
    params: Dict[str, Any] = {
        "type": "earnings-calendar",
        "limit": limit,
        "date_after": date_after or today.strftime("%Y-%m-%d"),
        "date_before": date_before or (today + timedelta(days=30)).strftime("%Y-%m-%d"),
    }
    if ticker:
        params["ticker"] = ticker.upper()

    data = _get("/calendar", params)
    if not data:
        return []
    # API returns list directly or {"earningsCalendar": [...]}
    if isinstance(data, list):
        return data
    return data.get("earningsCalendar", data.get("data", []))


def get_historical_earnings(ticker: str, limit: int = 8) -> List[Dict]:
    """Past earnings results (EPS actual vs estimate, revenue beats/misses)."""
    data = _get("/calendar", {"type": "earnings", "ticker": ticker.upper(), "limit": limit})
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("earnings", data.get("data", []))


def get_earnings_transcript(ticker: str) -> Optional[Dict]:
    """Latest earnings call transcript for a ticker."""
    data = _get("/calendar", {"type": "transcript-latest", "ticker": ticker.upper()})
    if not data:
        return None
    if isinstance(data, list):
        return data[0] if data else None
    return data


# ── Company Fundamentals ───────────────────────────────────────────────────────

def get_key_metrics_ttm(ticker: str) -> Optional[Dict]:
    """
    Trailing-twelve-months key metrics.
    Key fields: peRatioTTM, priceToSalesRatioTTM, pbRatioTTM,
                roeTTM, roicTTM, debtToEquityTTM, freeCashFlowYieldTTM.
    """
    data = _get("/financial-statements", {
        "type": "key-metrics-ttm",
        "ticker": ticker.upper(),
    })
    if not data:
        return None
    if isinstance(data, list):
        return data[0] if data else None
    return data


def get_income_statement(ticker: str, period: str = "annual", limit: int = 4) -> List[Dict]:
    """Income statement — revenue, gross profit, net income, EPS."""
    data = _get("/financial-statements", {
        "type": "income-statement",
        "ticker": ticker.upper(),
        "period": period,
        "limit": limit,
    })
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("incomeStatement", data.get("data", []))


def get_company_profile(ticker: str) -> Optional[Dict]:
    """Company profile: description, sector, industry, employees, market cap."""
    data = _get("/company-details", {"type": "profile", "ticker": ticker.upper()})
    if not data:
        return None
    if isinstance(data, list):
        return data[0] if data else None
    return data


# ── Analyst Estimates & Targets ────────────────────────────────────────────────

def get_analyst_estimates(ticker: str) -> Optional[Dict]:
    """
    Forward EPS and revenue estimates (consensus analyst view).
    Fields: estimatedEpsAvg, estimatedRevenueAvg, numberAnalystEstimatedRevenue.
    """
    data = _get("/analyst", {"type": "estimates", "ticker": ticker.upper()})
    if not data:
        return None
    if isinstance(data, list):
        return data[0] if data else None
    return data


def get_price_targets(ticker: str) -> Optional[Dict]:
    """
    Analyst price target summary.
    Fields: lastMonth (consensus), high, low, median, numberOfAnalysts.
    """
    data = _get("/analyst", {"type": "price-target-summary", "ticker": ticker.upper()})
    if not data:
        return None
    if isinstance(data, list):
        return data[0] if data else None
    return data


# ── SEC Filings ────────────────────────────────────────────────────────────────

def get_sec_filings(
    ticker: str,
    filing_type: str = "8-K",
    limit: int = 10,
) -> List[Dict]:
    """
    Recent SEC filings for a ticker.
    filing_type: "8-K", "10-Q", "10-K", "4" (insider), etc.
    Fields: date, type, link, finalLink.
    """
    data = _get("/filings", {
        "type": "filings",
        "ticker": ticker.upper(),
        "formType": filing_type,
        "limit": limit,
    })
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("filings", data.get("data", []))


# ── Congressional Trades ───────────────────────────────────────────────────────

def get_congressional_trades(ticker: Optional[str] = None, limit: int = 20) -> List[Dict]:
    """
    Congressional trading disclosures (Senate/House).
    Fields: transactionDate, ticker, representative, type, amount, party.
    """
    params: Dict[str, Any] = {"type": "senate-trading", "limit": limit}
    if ticker:
        params["ticker"] = ticker.upper()
    data = _get("/alternative-data", params)
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("senateTradingData", data.get("data", []))


# ── Economic Calendar ──────────────────────────────────────────────────────────

def get_economic_calendar(
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    limit: int = 30,
) -> List[Dict]:
    """
    Upcoming macro events: Fed decisions, CPI, GDP, employment reports.
    Fields: event, date, country, impact, actual, previous, estimate.
    """
    today = datetime.now(timezone.utc)
    data = _get("/calendar", {
        "type": "economic-calendar",
        "date_after": date_after or today.strftime("%Y-%m-%d"),
        "date_before": date_before or (today + timedelta(days=14)).strftime("%Y-%m-%d"),
        "limit": limit,
    })
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("economicCalendar", data.get("data", []))


# ── Options Flow ───────────────────────────────────────────────────────────────

def get_options_flow(ticker: str, limit: int = 20) -> List[Dict]:
    """
    Unusual options activity / dark pool prints for a ticker.
    Fields: date, putCall, strike, expiry, totalPremium, sentiment.
    """
    data = _get("/options", {
        "type": "unusual-activity",
        "ticker": ticker.upper(),
        "limit": limit,
    })
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("unusualActivity", data.get("data", []))


# ── Composite equity research snapshot ────────────────────────────────────────

def get_equity_snapshot(ticker: str) -> Dict:
    """
    One-shot equity research snapshot combining:
      • TTM key metrics
      • Analyst price targets
      • Recent earnings history (last 4 quarters)
      • Company profile excerpt

    Returns a merged dict — all keys are optional (may be None if API down).
    """
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        f_metrics  = pool.submit(get_key_metrics_ttm, ticker)
        f_targets  = pool.submit(get_price_targets, ticker)
        f_earnings = pool.submit(get_historical_earnings, ticker, 4)
        f_profile  = pool.submit(get_company_profile, ticker)

    metrics  = f_metrics.result()
    targets  = f_targets.result()
    earnings = f_earnings.result()
    profile  = f_profile.result()

    return {
        "ticker":           ticker.upper(),
        "key_metrics_ttm":  metrics,
        "price_targets":    targets,
        "earnings_history": earnings,
        "profile":          profile,
        "source":           "funda.ai",
    }
