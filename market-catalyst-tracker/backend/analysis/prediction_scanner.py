"""
Prediction Market Scanner
==========================
Queries Polymarket's public APIs to surface three categories of markets
that directly relate to our existing data streams:

1. BIOTECH / FDA MARKETS
   Polymarket markets about drug approvals, FDA decisions, clinical trials.
   These are the prediction-market counterpart to our catalyst calendar —
   showing crowd-implied probability alongside our SEC/FDA sourced data.

2. MACRO MARKETS
   Fed rate decisions, CPI outcomes, recession probability, tariffs.
   These enrich the "Why is the market moving?" correlator by showing
   what traders are pricing in for future macro events.

3. CATALYST ENRICHMENT
   For each CatalystEvent from catalyst_scanner.py, we try to find a
   matching Polymarket market and attach the crowd's probability to it.
   e.g. MRNA PDUFA → search "Moderna FDA" → attach yes_price = 0.72.

4. GENERAL TOP MARKETS (by volume)
   Gives a broad real-time pulse of what the crowd is focused on.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from data.polymarket_adapter import (
    get_top_markets,
    get_markets_by_tag,
    search_markets,
    enrich_with_live_price,
    get_price_history,
)
from models.schemas import CatalystEvent

logger = logging.getLogger(__name__)

# ── Category keyword filters ──────────────────────────────────────────────────

_BIOTECH_KEYWORDS = [
    "fda", "drug", "approval", "clinical trial", "phase 3", "phase iii",
    "nda", "bla", "pdufa", "biotech", "pharmaceutical", "cancer", "oncology",
    "gene therapy", "mrna", "immunotherapy", "alzheimer", "diabetes",
    "weight loss", "glp-1", "ozempic", "wegovy", "crispr", "gene editing",
]

_MACRO_KEYWORDS = [
    "fed", "federal reserve", "interest rate", "rate cut", "rate hike",
    "inflation", "cpi", "recession", "gdp", "unemployment", "jobs",
    "tariff", "trade", "debt ceiling", "treasury", "yield", "dollar",
    "quantitative", "fomc", "powell",
]

_GEOPOLITICAL_KEYWORDS = [
    "trump", "election", "war", "ukraine", "russia", "china", "taiwan",
    "nato", "sanctions", "oil", "opec",
]


def _keyword_match(question: str, keywords: List[str]) -> bool:
    q = question.lower()
    return any(kw in q for kw in keywords)


def _score_relevance(question: str, keywords: List[str]) -> int:
    q = question.lower()
    return sum(1 for kw in keywords if kw in q)


# ── Market categoriser ────────────────────────────────────────────────────────

def classify_market(market: Dict) -> str:
    """Classify a Polymarket market into our taxonomy."""
    q = market.get("question", "")
    tags = market.get("tags", [])

    # Tag-based (most reliable)
    if any(t in ("science", "health") for t in tags):
        return "biotech_fda"
    if any(t in ("economics",) for t in tags):
        return "macro"
    if any(t in ("politics",) for t in tags):
        return "geopolitical"
    if any(t in ("crypto",) for t in tags):
        return "crypto"

    # Keyword fallback
    if _keyword_match(q, _BIOTECH_KEYWORDS):
        return "biotech_fda"
    if _keyword_match(q, _MACRO_KEYWORDS):
        return "macro"
    if _keyword_match(q, _GEOPOLITICAL_KEYWORDS):
        return "geopolitical"
    return "other"


# ── Catalyst enrichment ───────────────────────────────────────────────────────

# Keyword fragments for each biotech stock that map to Polymarket search terms
_TICKER_SEARCH_TERMS: Dict[str, List[str]] = {
    "MRNA":  ["Moderna", "mRNA vaccine"],
    "BNTX":  ["BioNTech"],
    "VRTX":  ["Vertex"],
    "REGN":  ["Regeneron"],
    "BIIB":  ["Biogen"],
    "GILD":  ["Gilead"],
    "AMGN":  ["Amgen"],
    "NVAX":  ["Novavax"],
    "CRSP":  ["CRISPR"],
    "EDIT":  ["Editas"],
    "NTLA":  ["Intellia"],
    "BLUE":  ["bluebird bio"],
    "SAGE":  ["Sage Therapeutics"],
    "ACAD":  ["ACADIA"],
    "MDGL":  ["Madrigal"],
    "RARE":  ["Ultragenyx"],
    "ARWR":  ["Arrowhead"],
    "BEAM":  ["Beam Therapeutics"],
    "FOLD":  ["Amicus"],
    "KYMR":  ["Kymera"],
    "PRAX":  ["Praxis"],
    "ALNY":  ["Alnylam"],
    "IONS":  ["Ionis"],
    "ARVN":  ["Arvinas"],
    "INCY":  ["Incyte"],
    "NBIX":  ["Neurocrine"],
}


async def find_matching_prediction_market(
    catalyst: CatalystEvent,
) -> Optional[Dict]:
    """
    Given a catalyst event, search Polymarket for a matching binary market.
    Returns the best match (highest volume) or None.
    """
    sym = catalyst.symbol
    company = catalyst.company or sym
    event_type = catalyst.event_type

    # Build search queries: company name + event type keywords
    queries: List[str] = []

    # Explicit search terms if we know the company
    known_terms = _TICKER_SEARCH_TERMS.get(sym, [])
    for term in known_terms:
        if "fda" in event_type.lower() or "pdufa" in event_type.lower():
            queries.append(f"{term} FDA approval")
            queries.append(f"{term} FDA")
        elif "phase3" in event_type.lower():
            queries.append(f"{term} clinical trial")
            queries.append(f"{term} phase 3")
        else:
            queries.append(term)

    # Generic: company name
    if company and company != sym:
        first_word = company.split()[0]
        if len(first_word) > 3:
            queries.append(f"{first_word} FDA")

    # Try each query, return first non-empty result with highest volume
    best: Optional[Dict] = None
    for q in queries[:4]:  # cap at 4 API calls per catalyst
        results = await search_markets(q, limit=5)
        if results:
            # Prefer active, non-closed, highest volume
            active = [m for m in results if m.get("active") and not m.get("closed")]
            pool = active or results
            pool.sort(key=lambda m: m.get("volume", 0), reverse=True)
            candidate = pool[0]
            if not best or candidate.get("volume", 0) > best.get("volume", 0):
                best = candidate
        await asyncio.sleep(0.05)  # gentle rate limit

    return best


async def enrich_catalysts_with_predictions(
    catalysts: List[CatalystEvent],
    max_enrichments: int = 20,
) -> List[Dict]:
    """
    For a list of CatalystEvents, attach Polymarket market data where available.
    Returns enriched dicts: {**catalyst.dict(), prediction_market: {...} | None}
    """
    enriched: List[Dict] = []
    # Only enrich high/medium priority events up to max_enrichments
    priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    sorted_cats = sorted(catalysts, key=lambda c: priority_order.get(c.priority, 2))

    tasks_done = 0
    for c in sorted_cats:
        base = c.model_dump()
        if tasks_done < max_enrichments:
            pm = await find_matching_prediction_market(c)
            if pm:
                pm = await enrich_with_live_price(pm)
            base["prediction_market"] = pm
            tasks_done += 1
        else:
            base["prediction_market"] = None
        enriched.append(base)

    return enriched


# ── Category fetchers ─────────────────────────────────────────────────────────

async def get_biotech_fda_markets(limit: int = 30) -> List[Dict]:
    """
    Pull Polymarket markets related to biotech/FDA events.
    Fetches from 'science' and 'health' tags, then keyword-filters.
    """
    sci, health = await asyncio.gather(
        get_markets_by_tag("science", limit=50),
        get_markets_by_tag("health",  limit=30),
    )
    combined: Dict[str, Dict] = {}
    for m in sci + health:
        cid = m.get("condition_id", "")
        if cid and cid not in combined:
            combined[cid] = m

    # Further filter by biotech keywords (remove generic science markets)
    relevant = [
        m for m in combined.values()
        if _keyword_match(m.get("question",""), _BIOTECH_KEYWORDS)
    ]
    relevant.sort(key=lambda m: m.get("volume", 0), reverse=True)
    return relevant[:limit]


async def get_macro_markets(limit: int = 30) -> List[Dict]:
    """
    Pull Polymarket markets about macro-economic events:
    Fed rates, inflation, recession, GDP, tariffs, trade deals.
    """
    econ = await get_markets_by_tag("economics", limit=50)
    # Also search directly
    fed_markets = await search_markets("Federal Reserve rate", limit=10)

    combined: Dict[str, Dict] = {}
    for m in econ + fed_markets:
        cid = m.get("condition_id", "")
        if cid and cid not in combined:
            combined[cid] = m

    macro = [
        m for m in combined.values()
        if _keyword_match(m.get("question",""), _MACRO_KEYWORDS)
    ]
    macro.sort(key=lambda m: m.get("volume", 0), reverse=True)
    return macro[:limit]


async def get_geopolitical_markets(limit: int = 20) -> List[Dict]:
    """Markets related to geopolitical events that move markets."""
    pol = await get_markets_by_tag("politics", limit=50)
    relevant = [
        m for m in pol
        if _keyword_match(m.get("question",""), _GEOPOLITICAL_KEYWORDS + _MACRO_KEYWORDS)
    ]
    relevant.sort(key=lambda m: m.get("volume", 0), reverse=True)
    return relevant[:limit]


async def get_top_markets_all(limit: int = 50) -> List[Dict]:
    """Top markets by volume, classified by category."""
    markets = await get_top_markets(limit=limit)
    for m in markets:
        m["category"] = classify_market(m)
    markets.sort(key=lambda m: m.get("volume", 0), reverse=True)
    return markets


# ── Price history for charts ──────────────────────────────────────────────────

async def get_market_chart_data(condition_id_or_token_id: str) -> List[Dict]:
    """
    Historical probability series for a market (for frontend charting).
    Uses the Gamma price-history endpoint via the yes_token_id.
    """
    return await get_price_history(condition_id_or_token_id, interval="1d", fidelity=1)


# ── Master pull ───────────────────────────────────────────────────────────────

async def pull_all_prediction_data() -> Dict:
    """
    Pull all prediction market categories concurrently.
    Returns a single dict with keys: top, biotech, macro, geopolitical.
    """
    top_task  = asyncio.create_task(get_top_markets_all(50))
    bio_task  = asyncio.create_task(get_biotech_fda_markets(30))
    mac_task  = asyncio.create_task(get_macro_markets(30))
    geo_task  = asyncio.create_task(get_geopolitical_markets(20))

    top, bio, mac, geo = await asyncio.gather(
        top_task, bio_task, mac_task, geo_task,
        return_exceptions=True,
    )

    return {
        "top":         top if isinstance(top, list) else [],
        "biotech_fda": bio if isinstance(bio, list) else [],
        "macro":       mac if isinstance(mac, list) else [],
        "geopolitical":geo if isinstance(geo, list) else [],
        "timestamp":   int(time.time()),
    }
