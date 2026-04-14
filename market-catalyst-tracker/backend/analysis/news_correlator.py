"""
News Correlator — "Why is the market moving?"
================================================
Pulls recent news, scores it against known macro driver categories,
cross-references it with actual price moves, and returns a human-readable
explanation of the primary market driver.

Completely free — uses Yahoo Finance news via yfinance plus optional Finnhub.

Driver taxonomy (MARKET_DRIVERS):
  fed_policy      → Fed decisions, FOMC, interest rates, Powell
  inflation        → CPI, PCE, PPI, inflation data
  jobs             → NFP, unemployment, payrolls, JOLTS
  earnings         → EPS beats/misses, guidance, revenue
  geopolitical     → Wars, tariffs, trade, sanctions, elections
  ai_tech          → AI, ChatGPT, semiconductor, GPU, Nvidia
  crypto           → Bitcoin, Ethereum, crypto regulation
  gdp_growth       → GDP, recession, growth, PMI
  banking          → Bank failures, credit, SVB-type events
  energy           → Oil, OPEC, natural gas, energy crisis
  healthcare_fda   → FDA approvals, drug trials, health policy
  other            → Catch-all
"""
from __future__ import annotations
import logging
import re
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from data.yfinance_adapter import get_market_news, get_news, get_quote
from data.finnhub_adapter import get_market_news as finnhub_market_news
from models.schemas import NewsCorrelation, NewsItem

logger = logging.getLogger(__name__)


# ── Driver keyword taxonomy ────────────────────────────────────────────────────

MARKET_DRIVERS: Dict[str, List[str]] = {
    "fed_policy": [
        "Federal Reserve", "Fed", "FOMC", "interest rate", "rate hike", "rate cut",
        "Jerome Powell", "Powell", "monetary policy", "quantitative tightening", "QT",
        "fed funds", "basis points", "bps", "hawkish", "dovish",
    ],
    "inflation": [
        "CPI", "consumer price index", "inflation", "PCE", "PPI", "producer price",
        "core inflation", "price pressure", "cost of living", "deflation",
    ],
    "jobs": [
        "jobs report", "nonfarm payrolls", "unemployment", "labor market",
        "JOLTS", "payrolls", "jobless claims", "employment", "wages", "ADP",
    ],
    "earnings": [
        "earnings", "EPS", "revenue", "profit", "quarterly results", "guidance",
        "beat expectations", "missed estimates", "net income", "operating income",
    ],
    "geopolitical": [
        "tariff", "trade war", "sanctions", "war", "conflict", "NATO",
        "Russia", "Ukraine", "China", "Taiwan", "Middle East", "election",
        "trade deal", "import", "export ban",
    ],
    "ai_tech": [
        "artificial intelligence", "AI", "ChatGPT", "OpenAI", "Nvidia", "GPU",
        "semiconductor", "chip", "machine learning", "large language model",
        "generative AI", "data center", "Microsoft", "Google", "Meta AI",
    ],
    "crypto": [
        "bitcoin", "Bitcoin", "BTC", "ethereum", "Ethereum", "ETH",
        "crypto", "cryptocurrency", "blockchain", "stablecoin", "SEC crypto",
        "digital asset", "crypto regulation",
    ],
    "gdp_growth": [
        "GDP", "gross domestic product", "recession", "economic growth",
        "PMI", "ISM", "manufacturing", "services sector", "contraction", "expansion",
    ],
    "banking": [
        "bank failure", "bank run", "credit", "lending", "SVB", "deposit",
        "financial crisis", "bailout", "FDIC", "bank stress", "regional bank",
    ],
    "energy": [
        "oil", "crude", "OPEC", "natural gas", "energy crisis", "gasoline",
        "WTI", "Brent", "refinery", "pipeline", "energy prices",
    ],
    "healthcare_fda": [
        "FDA", "drug approval", "clinical trial", "biotech", "pharmaceutical",
        "PDUFA", "NDA", "BLA", "Ozempic", "weight loss drug", "cancer drug",
        "Medicare", "Medicaid", "healthcare reform",
    ],
}

DRIVER_LABELS: Dict[str, str] = {
    "fed_policy": "Federal Reserve / Interest Rates",
    "inflation": "Inflation Data (CPI / PCE)",
    "jobs": "Jobs Market",
    "earnings": "Earnings Results",
    "geopolitical": "Geopolitical Events",
    "ai_tech": "AI & Technology",
    "crypto": "Crypto Markets",
    "gdp_growth": "Economic Growth / GDP",
    "banking": "Banking / Credit",
    "energy": "Energy & Oil",
    "healthcare_fda": "Healthcare / FDA",
    "other": "General Market",
}

# Sentiment word lists for simple scoring
_BULLISH_WORDS = [
    "surges", "soars", "rallies", "jumps", "gains", "beat", "beats", "exceeded",
    "better-than-expected", "record high", "upgrade", "bullish", "boom", "roars",
    "optimism", "recovery", "approved", "approval", "breakthrough",
]
_BEARISH_WORDS = [
    "plunges", "slides", "drops", "falls", "sinks", "misses", "missed", "below",
    "worse-than-expected", "downgrade", "bearish", "crash", "recession", "tariff",
    "rejected", "CRL", "complete response letter", "concern", "worry", "fear",
    "layoffs", "warning", "disappoints",
]


# ── Scoring helpers ────────────────────────────────────────────────────────────

def _sentiment_score(text: str) -> float:
    """Returns -1.0 (bearish) → +1.0 (bullish)."""
    t = text.lower()
    bull = sum(1 for w in _BULLISH_WORDS if w.lower() in t)
    bear = sum(1 for w in _BEARISH_WORDS if w.lower() in t)
    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 3)


def _categorize_item(item: Dict) -> str:
    """Return the best-match driver category for a news item."""
    text = (item.get("headline", "") + " " + item.get("summary", "")).lower()
    scores: Dict[str, int] = defaultdict(int)
    for driver, keywords in MARKET_DRIVERS.items():
        for kw in keywords:
            if kw.lower() in text:
                scores[driver] += 1
    if not scores:
        return "other"
    return max(scores, key=scores.__getitem__)


def _enrich_news_items(raw_items: List[Dict]) -> List[NewsItem]:
    """Convert raw news dicts to NewsItem with sentiment and category."""
    enriched: List[NewsItem] = []
    for n in raw_items:
        headline = n.get("headline") or n.get("title", "")
        if not headline:
            continue
        driver = _categorize_item(n)
        sentiment = _sentiment_score(headline + " " + n.get("summary", ""))
        enriched.append(NewsItem(
            id=str(n.get("id", "")),
            headline=headline,
            summary=n.get("summary", "")[:300],
            source=n.get("source", ""),
            url=n.get("url", ""),
            timestamp=n.get("timestamp") or n.get("providerPublishTime") or int(time.time()),
            sentiment_score=sentiment,
            driver_category=driver,
            related_symbols=n.get("related_symbols", []),
        ))
    return enriched


def _build_summary(driver: str, market_change_pct: float, top_news: List[NewsItem]) -> str:
    """Generate a plain-language summary sentence."""
    label = DRIVER_LABELS.get(driver, "general news")
    direction = "rising" if market_change_pct > 0 else "falling"
    magnitude = abs(market_change_pct)
    mag_word = "sharply" if magnitude > 1.5 else "modestly" if magnitude > 0.4 else "slightly"

    headlines = [n.headline for n in top_news[:2]]
    hl_str = f' — "{headlines[0]}"' if headlines else ""
    return (
        f"Markets are {mag_word} {direction} ({market_change_pct:+.2f}%) "
        f"driven primarily by {label}{hl_str}."
    )


# ── Main entry point ───────────────────────────────────────────────────────────

def correlate_market_news(market_symbol: str = "SPY") -> NewsCorrelation:
    """
    Pull live market news, classify it, and identify the primary driver
    of today's market move.
    """
    # 1. Get current market quote
    quote = get_quote(market_symbol) or {}
    market_change_pct = quote.get("change_pct", 0.0)

    # 2. Gather news from multiple free sources
    raw_news: List[Dict] = []
    raw_news += get_market_news(limit=30)           # Yahoo Finance (SPY, QQQ, IWM)

    # Finnhub supplements if key is configured
    try:
        raw_news += finnhub_market_news(category="general", limit=20)
    except Exception:
        pass

    # Deduplicate by headline
    seen: set = set()
    deduped: List[Dict] = []
    for n in raw_news:
        h = n.get("headline", "")
        if h and h not in seen:
            seen.add(h)
            deduped.append(n)

    # 3. Enrich with sentiment + category
    enriched = _enrich_news_items(deduped)

    # 4. Count driver category frequencies (weighted by recency & sentiment)
    driver_counter: Counter = Counter()
    now_ts = int(time.time())
    for item in enriched:
        # Recency weight: full credit in last 6h, halved per 6h after
        age_hours = max(0, (now_ts - item.timestamp) / 3600)
        recency_weight = max(0.1, 1.0 / (1 + age_hours / 6))
        driver_counter[item.driver_category] += recency_weight

    primary_driver = driver_counter.most_common(1)[0][0] if driver_counter else "other"

    # 5. Compute confidence: what fraction of coverage the top driver represents
    total_weight = sum(driver_counter.values()) or 1
    confidence = round(driver_counter[primary_driver] / total_weight, 3)

    # 6. Sort news: primary driver articles first, then by recency
    enriched.sort(
        key=lambda x: (
            x.driver_category != primary_driver,   # primary driver first (False < True)
            -x.timestamp,
        )
    )

    summary = _build_summary(primary_driver, market_change_pct, enriched[:3])

    return NewsCorrelation(
        market_symbol=market_symbol,
        market_change_pct=round(market_change_pct, 3),
        primary_driver=primary_driver,
        driver_label=DRIVER_LABELS.get(primary_driver, primary_driver),
        confidence=confidence,
        summary=summary,
        supporting_news=enriched[:15],
        timestamp=int(time.time()),
    )


def correlate_symbol_news(symbol: str) -> Dict:
    """
    For a specific stock: explain why it's moving today.
    Returns correlation dict similar to market-wide but symbol-focused.
    """
    quote = get_quote(symbol) or {}
    change_pct = quote.get("change_pct", 0.0)
    raw = get_news(symbol, limit=15)
    enriched = _enrich_news_items(raw)

    driver_counter: Counter = Counter()
    for item in enriched:
        driver_counter[item.driver_category] += 1

    primary = driver_counter.most_common(1)[0][0] if driver_counter else "other"
    total = sum(driver_counter.values()) or 1
    confidence = round(driver_counter[primary] / total, 3)

    enriched.sort(key=lambda x: -x.timestamp)
    summary = _build_summary(primary, change_pct, enriched[:2])

    return {
        "symbol": symbol,
        "change_pct": round(change_pct, 3),
        "primary_driver": primary,
        "driver_label": DRIVER_LABELS.get(primary, primary),
        "confidence": confidence,
        "summary": summary,
        "news": [n.model_dump() for n in enriched[:10]],
        "timestamp": int(time.time()),
    }
