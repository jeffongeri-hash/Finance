"""
Adanos Finance Sentiment API Adapter
======================================
Python client for https://api.adanos.org

Provides cross-source sentiment signals for stocks:
  • Reddit   — mentions, buzz_score, bullish_pct, trend
  • X (Twitter) — mentions, buzz_score, bullish_pct, trend
  • News     — mentions, buzz_score, bullish_pct, trend
  • Polymarket — trade_count, buzz_score, bullish_pct, trend

Auth: X-API-Key header
All calls degrade gracefully — returns empty data when key is absent.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Any

import httpx

from config import ADANOS_API_KEY, HTTP_TIMEOUT

logger = logging.getLogger(__name__)

_BASE = "https://api.adanos.org"

_SOURCES = ("reddit", "x", "news", "polymarket")


def _headers() -> Dict[str, str]:
    return {"X-API-Key": ADANOS_API_KEY}


def _get(path: str, params: Dict[str, Any] | None = None) -> Any:
    """GET helper. Returns parsed JSON or None on failure."""
    if not ADANOS_API_KEY:
        return None
    url = f"{_BASE}{path}"
    try:
        r = httpx.get(url, headers=_headers(), params=params or {}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        logger.warning("Adanos API %s → HTTP %d", url, e.response.status_code)
    except Exception as e:
        logger.debug("Adanos API %s → %s", url, e)
    return None


# ── Per-source compare endpoints ───────────────────────────────────────────────

def _compare(source: str, tickers: List[str], days: int = 7) -> List[Dict]:
    """
    Call /reddit|x|news|polymarket/stocks/v1/compare for one or more tickers.
    Returns list of per-ticker sentiment dicts.
    """
    path = f"/{source}/stocks/v1/compare"
    params = {"tickers": ",".join(t.upper() for t in tickers), "days": days}
    data = _get(path, params)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    # Some endpoints wrap in {"data": [...]}
    return data.get("data", [])


def get_reddit_sentiment(tickers: List[str], days: int = 7) -> List[Dict]:
    """Reddit sentiment for one or more tickers."""
    return _compare("reddit", tickers, days)


def get_x_sentiment(tickers: List[str], days: int = 7) -> List[Dict]:
    """X (Twitter) sentiment for one or more tickers."""
    return _compare("x", tickers, days)


def get_news_sentiment(tickers: List[str], days: int = 7) -> List[Dict]:
    """News sentiment for one or more tickers."""
    return _compare("news", tickers, days)


def get_polymarket_sentiment(tickers: List[str], days: int = 7) -> List[Dict]:
    """Polymarket trading sentiment for one or more tickers."""
    return _compare("polymarket", tickers, days)


# ── Multi-source snapshot ──────────────────────────────────────────────────────

def get_sentiment_snapshot(ticker: str, days: int = 7) -> Dict:
    """
    Full cross-source sentiment snapshot for a single ticker.
    Fetches Reddit, X, News, and Polymarket in parallel.

    Returns:
      {
        "ticker": "TSLA",
        "sources": {
          "reddit":     { "buzz_score": 74, "bullish_pct": 0.31, "mentions": 647, "trend": "rising" },
          "x":          { ... },
          "news":       { ... },
          "polymarket": { "buzz_score": 45, "bullish_pct": 0.60, "trade_count": 1200, "trend": "stable" },
        },
        "composite": {
          "buzz_score":   float,   # average across available sources
          "bullish_pct":  float,   # weighted average bullish %
          "sentiment":    str,     # "bullish" | "bearish" | "neutral" | "mixed"
          "sources_agree": bool,
        }
      }
    """
    import concurrent.futures

    results: Dict[str, List[Dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_compare, src, [ticker], days): src
            for src in _SOURCES
        }
        for fut, src in futures.items():
            try:
                items = fut.result(timeout=10)
                # Find the entry matching our ticker
                match = next(
                    (i for i in items if (i.get("ticker") or "").upper() == ticker.upper()),
                    items[0] if items else None,
                )
                if match:
                    results[src] = match
            except Exception as e:
                logger.debug("Adanos %s/%s: %s", src, ticker, e)

    # Build composite
    buzz_scores: List[float] = []
    bull_pcts: List[float] = []
    for src_data in results.values():
        b = src_data.get("buzz_score")
        p = src_data.get("bullish_pct")
        if b is not None:
            buzz_scores.append(float(b))
        if p is not None:
            bull_pcts.append(float(p))

    avg_buzz  = round(sum(buzz_scores) / len(buzz_scores), 1) if buzz_scores else None
    avg_bull  = round(sum(bull_pcts) / len(bull_pcts), 3) if bull_pcts else None

    # Sentiment label
    sentiment = "neutral"
    if avg_bull is not None:
        if avg_bull >= 0.60:
            sentiment = "bullish"
        elif avg_bull <= 0.35:
            sentiment = "bearish"
        else:
            # Check spread — if sources disagree significantly it's "mixed"
            spread = max(bull_pcts) - min(bull_pcts) if len(bull_pcts) > 1 else 0
            sentiment = "mixed" if spread > 0.25 else "neutral"

    # Do sources agree (all in same direction)?
    sources_agree = False
    if len(bull_pcts) >= 2:
        all_bull = all(p > 0.55 for p in bull_pcts)
        all_bear = all(p < 0.40 for p in bull_pcts)
        sources_agree = all_bull or all_bear

    return {
        "ticker":    ticker.upper(),
        "days":      days,
        "sources":   results,
        "composite": {
            "buzz_score":    avg_buzz,
            "bullish_pct":   avg_bull,
            "sentiment":     sentiment,
            "sources_agree": sources_agree,
        },
        "available": bool(ADANOS_API_KEY),
    }


def get_batch_sentiment(tickers: List[str], days: int = 7) -> Dict[str, Dict]:
    """
    Lightweight batch sentiment across all sources for multiple tickers.
    Uses compare endpoints which support comma-separated ticker lists.
    Returns dict keyed by ticker.

    This is more efficient than calling get_sentiment_snapshot() per ticker
    when you need a ranked sentiment table.
    """
    import concurrent.futures

    all_source_data: Dict[str, List[Dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_compare, src, tickers, days): src
            for src in _SOURCES
        }
        for fut, src in futures.items():
            try:
                all_source_data[src] = fut.result(timeout=10)
            except Exception:
                all_source_data[src] = []

    # Pivot: ticker → {source: data}
    by_ticker: Dict[str, Dict] = {}
    for src, items in all_source_data.items():
        for item in items:
            tk = (item.get("ticker") or "").upper()
            if not tk:
                continue
            if tk not in by_ticker:
                by_ticker[tk] = {"ticker": tk, "sources": {}}
            by_ticker[tk]["sources"][src] = item

    # Add composite per ticker
    for tk, td in by_ticker.items():
        buzz_scores: List[float] = []
        bull_pcts: List[float] = []
        for src_data in td["sources"].values():
            b = src_data.get("buzz_score")
            p = src_data.get("bullish_pct")
            if b is not None:
                buzz_scores.append(float(b))
            if p is not None:
                bull_pcts.append(float(p))

        avg_buzz = round(sum(buzz_scores) / len(buzz_scores), 1) if buzz_scores else None
        avg_bull = round(sum(bull_pcts) / len(bull_pcts), 3) if bull_pcts else None

        sentiment = "neutral"
        if avg_bull is not None:
            if avg_bull >= 0.60:
                sentiment = "bullish"
            elif avg_bull <= 0.35:
                sentiment = "bearish"

        td["composite"] = {
            "buzz_score":  avg_buzz,
            "bullish_pct": avg_bull,
            "sentiment":   sentiment,
        }

    return by_ticker
