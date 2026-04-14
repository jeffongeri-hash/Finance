"""
Finnhub adapter — optional enhancement layer.
When FINNHUB_API_KEY is set, augments news with sentiment scores and gives
access to company peers, FDA-related events, and earnings calendars.
When key is absent, all functions return empty lists gracefully.
Free tier: 60 API calls / minute.
"""
from __future__ import annotations
import logging
import time
from typing import Dict, List, Optional

from config import FINNHUB_API_KEY, HTTP_TIMEOUT

logger = logging.getLogger(__name__)


def _client():
    """Lazy import + instantiate finnhub client only when key is available."""
    if not FINNHUB_API_KEY:
        return None
    try:
        import finnhub
        return finnhub.Client(api_key=FINNHUB_API_KEY)
    except ImportError:
        logger.warning("finnhub-python not installed")
        return None


# ── News + Sentiment ───────────────────────────────────────────────────────────

def get_company_news(symbol: str, days_back: int = 7) -> List[Dict]:
    """
    Company news with headline, summary, source, URL, timestamp.
    Falls back to empty list if no key configured.
    """
    c = _client()
    if not c:
        return []
    try:
        from_date = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days_back * 86400))
        to_date = time.strftime("%Y-%m-%d", time.gmtime())
        raw = c.company_news(symbol, _from=from_date, to=to_date)
        return [
            {
                "id": str(n.get("id", "")),
                "headline": n.get("headline", ""),
                "summary": n.get("summary", ""),
                "source": n.get("source", ""),
                "url": n.get("url", ""),
                "timestamp": n.get("datetime", int(time.time())),
                "related_symbols": [symbol],
            }
            for n in (raw or [])[:20]
            if n.get("headline")
        ]
    except Exception as e:
        logger.debug("Finnhub company_news %s: %s", symbol, e)
        return []


def get_news_sentiment(symbol: str) -> Optional[Dict]:
    """
    Overall news sentiment score for a symbol (-1 … +1 mapped from Finnhub buzz).
    Returns None if not available.
    """
    c = _client()
    if not c:
        return None
    try:
        raw = c.news_sentiment(symbol)
        if not raw:
            return None
        return {
            "symbol": symbol,
            "bullish_pct": raw.get("sentiment", {}).get("bullishPercent", 0.5),
            "bearish_pct": raw.get("sentiment", {}).get("bearishPercent", 0.5),
            "buzz_score": raw.get("buzz", {}).get("buzz", 0),
            "articles_count": raw.get("buzz", {}).get("articlesInLastWeek", 0),
            "score": raw.get("companyNewsScore", 0),
        }
    except Exception as e:
        logger.debug("Finnhub sentiment %s: %s", symbol, e)
        return None


def get_market_news(category: str = "general", limit: int = 20) -> List[Dict]:
    """
    General market news (category: 'general', 'forex', 'crypto', 'merger').
    """
    c = _client()
    if not c:
        return []
    try:
        raw = c.general_news(category) or []
        return [
            {
                "id": str(n.get("id", "")),
                "headline": n.get("headline", ""),
                "summary": n.get("summary", ""),
                "source": n.get("source", ""),
                "url": n.get("url", ""),
                "timestamp": n.get("datetime", int(time.time())),
                "related_symbols": [],
            }
            for n in raw[:limit]
            if n.get("headline")
        ]
    except Exception as e:
        logger.debug("Finnhub market news: %s", e)
        return []


# ── Peers & Recommendation ────────────────────────────────────────────────────

def get_peers(symbol: str) -> List[str]:
    c = _client()
    if not c:
        return []
    try:
        return c.company_peers(symbol) or []
    except Exception:
        return []


def get_recommendation_trends(symbol: str) -> Optional[Dict]:
    c = _client()
    if not c:
        return None
    try:
        raw = c.recommendation_trends(symbol)
        if raw:
            latest = raw[0]
            return {
                "strong_buy": latest.get("strongBuy", 0),
                "buy": latest.get("buy", 0),
                "hold": latest.get("hold", 0),
                "sell": latest.get("sell", 0),
                "strong_sell": latest.get("strongSell", 0),
                "period": latest.get("period", ""),
            }
    except Exception:
        pass
    return None


# ── Earnings Calendar (non-earnings catalysts only — per user request) ─────────

def get_upcoming_earnings(symbol: str) -> Optional[str]:
    """Returns next earnings date string or None. Informational only."""
    c = _client()
    if not c:
        return None
    try:
        raw = c.company_earnings(symbol, limit=1)
        if raw:
            return raw[0].get("period")
    except Exception:
        pass
    return None


# ── Quote (supplement) ────────────────────────────────────────────────────────

def get_quote(symbol: str) -> Optional[Dict]:
    """Real-time quote from Finnhub (useful as a yfinance fallback)."""
    c = _client()
    if not c:
        return None
    try:
        raw = c.quote(symbol)
        if not raw or raw.get("c") == 0:
            return None
        price = raw["c"]
        prev_close = raw.get("pc", price)
        return {
            "symbol": symbol,
            "price": price,
            "change": raw.get("d", 0),
            "change_pct": raw.get("dp", 0),
            "high": raw.get("h"),
            "low": raw.get("l"),
            "open": raw.get("o"),
            "prev_close": prev_close,
            "timestamp": raw.get("t", int(time.time())),
        }
    except Exception as e:
        logger.debug("Finnhub quote %s: %s", symbol, e)
        return None
