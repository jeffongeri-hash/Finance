"""
Biotech / FDA Catalyst Scanner
================================
Combines three free public data sources to build an upcoming catalyst calendar:

1. FDA public API (api.fda.gov)          → recent NDA/BLA approval actions
2. SEC EDGAR full-text search (EFTS)     → 8-K filings mentioning "PDUFA"
3. ClinicalTrials.gov API v2             → Phase 3 trials near primary endpoint

Cross-references results against our biotech universe (yfinance) to attach
live stock data (price, change, short interest, market cap) so the frontend
can surface the highest-conviction setups.
"""
from __future__ import annotations
import asyncio
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from config import BIOTECH_TICKERS, HTTP_TIMEOUT
from data.fda_adapter import pull_all_catalyst_data
from data.yfinance_adapter import get_quote
from models.schemas import CatalystEvent

logger = logging.getLogger(__name__)

# ── Priority scoring ───────────────────────────────────────────────────────────

_EVENT_BASE_SCORE = {
    "FDA_PDUFA": 90,
    "FDA_ADCOM": 80,
    "NDA_SUBMISSION": 60,
    "PHASE3_RESULT": 70,
    "SEC_8K_FDA": 50,
    "SEC_8K_APPROVAL": 85,
    "SEC_8K_CRL": 55,
}

_PRIORITY_THRESHOLDS = {"HIGH": 75, "MEDIUM": 45, "LOW": 0}


def _score_to_priority(score: float) -> str:
    for level, threshold in _PRIORITY_THRESHOLDS.items():
        if score >= threshold:
            return level
    return "LOW"


def _days_until(date_str: Optional[str]) -> Optional[int]:
    """Parse a date string and return days until that date (can be negative if past)."""
    if not date_str:
        return None
    formats = ["%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%Y-%m"]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str[:10], fmt[:len(date_str)])
            delta = (dt.date() - datetime.now(timezone.utc).date()).days
            return delta
        except ValueError:
            continue
    return None


def _score_event(
    event_type: str,
    days_until_val: Optional[int],
    market_cap: Optional[float],
    short_pct: Optional[float],
) -> float:
    """Composite score 0–100 for catalyst priority."""
    base = _EVENT_BASE_SCORE.get(event_type, 40)

    # Proximity boost: events within 30 days score higher
    prox_bonus = 0
    if days_until_val is not None:
        if 0 <= days_until_val <= 14:
            prox_bonus = 20
        elif 0 <= days_until_val <= 30:
            prox_bonus = 12
        elif 0 <= days_until_val <= 90:
            prox_bonus = 5
        elif days_until_val < 0:
            prox_bonus = -10  # past event

    # Small/micro cap bonus (higher volatility potential)
    cap_bonus = 0
    if market_cap:
        if market_cap < 500_000_000:       # < $500M micro cap
            cap_bonus = 10
        elif market_cap < 2_000_000_000:   # < $2B small cap
            cap_bonus = 5

    # High short interest → potential squeeze on approval
    si_bonus = 0
    if short_pct and short_pct > 20:
        si_bonus = 8
    elif short_pct and short_pct > 10:
        si_bonus = 4

    return min(100, base + prox_bonus + cap_bonus + si_bonus)


# ── Parsers ────────────────────────────────────────────────────────────────────

def _parse_fda_approvals(raw_list: List[Dict], stock_cache: Dict[str, Dict]) -> List[CatalystEvent]:
    events: List[CatalystEvent] = []
    for r in raw_list:
        subs = r.get("submissions", [])
        # Find the approval submission
        ap_sub = None
        for s in subs:
            if s.get("submission_status") == "AP":
                ap_sub = s
                break
        if not ap_sub:
            continue

        brand = r.get("brand_name") or ""
        generic = r.get("generic_name") or ""
        sponsor = r.get("sponsor_name") or ""
        status_date = ap_sub.get("submission_status_date", "")

        # Try to match sponsor to a ticker in our biotech universe
        ticker = _fuzzy_match_sponsor(sponsor, list(stock_cache.keys()))
        if not ticker:
            continue

        sq = stock_cache.get(ticker, {})
        days = _days_until(status_date)
        event_type = "SEC_8K_APPROVAL" if days and days < 0 else "FDA_PDUFA"
        score = _score_event(event_type, days, sq.get("market_cap"), sq.get("short_interest_pct"))

        events.append(CatalystEvent(
            symbol=ticker,
            company=sponsor,
            event_type=event_type,
            event_date=status_date[:10] if status_date else None,
            days_until=days,
            description=f"FDA Approval — {brand or generic} ({ap_sub.get('submission_type', 'NDA')})",
            priority=_score_to_priority(score),
            market_cap=sq.get("market_cap"),
            price=sq.get("price"),
            price_change_pct=sq.get("change_pct"),
            short_interest_pct=sq.get("short_interest_pct"),
            source_url="https://www.accessdata.fda.gov/scripts/cder/daf/",
        ))
    return events


def _parse_pdufa_filings(filings: List[Dict], stock_cache: Dict[str, Dict]) -> List[CatalystEvent]:
    events: List[CatalystEvent] = []
    for f in filings:
        tickers = f.get("tickers", [])
        if not tickers:
            continue
        for ticker in tickers:
            if ticker not in stock_cache and ticker not in BIOTECH_TICKERS:
                continue
            sq = stock_cache.get(ticker, {})
            file_date = f.get("file_date", "")
            score = _score_event("FDA_PDUFA", None, sq.get("market_cap"), sq.get("short_interest_pct"))
            events.append(CatalystEvent(
                symbol=ticker,
                company=f.get("entity", ticker),
                event_type="FDA_PDUFA",
                event_date=None,
                days_until=None,
                description=f"PDUFA date disclosed in SEC 8-K filing ({file_date})",
                priority=_score_to_priority(score),
                market_cap=sq.get("market_cap"),
                price=sq.get("price"),
                price_change_pct=sq.get("change_pct"),
                source_url=f.get("source_url", "https://efts.sec.gov/"),
            ))
    return events


def _parse_drug_filings(filings: List[Dict], stock_cache: Dict[str, Dict]) -> List[CatalystEvent]:
    events: List[CatalystEvent] = []
    for f in filings:
        tickers = f.get("tickers", [])
        for ticker in tickers:
            sq = stock_cache.get(ticker, {})
            file_date = f.get("file_date", "")
            headline = f.get("headline", "FDA-related filing")
            crl = "CRL" in headline or "Complete Response" in headline
            event_type = "SEC_8K_CRL" if crl else "SEC_8K_FDA"
            score = _score_event(event_type, None, sq.get("market_cap"), sq.get("short_interest_pct"))
            events.append(CatalystEvent(
                symbol=ticker,
                company=f.get("entity", ticker),
                event_type=event_type,
                event_date=file_date[:10] if file_date else None,
                days_until=_days_until(file_date),
                description=headline,
                priority=_score_to_priority(score),
                market_cap=sq.get("market_cap"),
                price=sq.get("price"),
                price_change_pct=sq.get("change_pct"),
                source_url=f.get("source_url", ""),
            ))
    return events


def _parse_phase3_trials(trials: List[Dict], stock_cache: Dict[str, Dict]) -> List[CatalystEvent]:
    """
    Match Phase 3 sponsors to known biotech tickers.
    """
    events: List[CatalystEvent] = []
    for trial in trials:
        proto = trial.get("protocolSection", {})
        id_mod = proto.get("identificationModule", {})
        status_mod = proto.get("statusModule", {})
        sponsor_mod = proto.get("sponsorCollaboratorsModule", {})
        desc_mod = proto.get("descriptionModule", {})

        title = id_mod.get("briefTitle") or id_mod.get("officialTitle") or ""
        nct_id = id_mod.get("nctId", "")
        sponsor = sponsor_mod.get("leadSponsor", {}).get("name", "")
        completion = status_mod.get("primaryCompletionDateStruct", {}).get("date", "")
        brief = desc_mod.get("briefSummary", "")[:300]

        ticker = _fuzzy_match_sponsor(sponsor, list(stock_cache.keys()))
        if not ticker:
            continue

        sq = stock_cache.get(ticker, {})
        days = _days_until(completion)
        score = _score_event("PHASE3_RESULT", days, sq.get("market_cap"), sq.get("short_interest_pct"))

        events.append(CatalystEvent(
            symbol=ticker,
            company=sponsor,
            event_type="PHASE3_RESULT",
            event_date=completion[:10] if completion else None,
            days_until=days,
            description=f"Phase 3 primary completion — {title[:120]}",
            priority=_score_to_priority(score),
            market_cap=sq.get("market_cap"),
            price=sq.get("price"),
            price_change_pct=sq.get("change_pct"),
            source_url=f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
        ))
    return events


# ── Ticker ↔ Sponsor fuzzy match ──────────────────────────────────────────────

def _build_name_map(stock_cache: Dict[str, Dict]) -> Dict[str, str]:
    """Build lowercase company name → ticker mapping."""
    m = {}
    for sym, data in stock_cache.items():
        name = data.get("name", sym).lower()
        m[name] = sym
        # Also index first word
        first = name.split()[0] if name.split() else ""
        if len(first) > 3:
            m[first] = sym
    return m


def _fuzzy_match_sponsor(sponsor: str, tickers: List[str]) -> Optional[str]:
    """
    Lightweight fuzzy match: check if any known ticker symbol appears in the
    sponsor name (case-insensitive) or if sponsor name starts match.
    """
    if not sponsor:
        return None
    s = sponsor.lower()

    # Direct ticker substring match (e.g. "Moderna" doesn't have "MRNA" but
    # "Vertex Pharmaceuticals" doesn't have "VRTX" either — so we can't rely
    # purely on this; still catches many cases)
    for ticker in tickers:
        if ticker.lower() in s:
            return ticker

    # Keyword match against common name fragments
    NAME_TO_TICKER = {
        "moderna": "MRNA", "biontech": "BNTX", "vertex": "VRTX",
        "regeneron": "REGN", "biogen": "BIIB", "gilead": "GILD",
        "amgen": "AMGN", "illumina": "ILMN", "biomarin": "BMRN",
        "alnylam": "ALNY", "exact sciences": "EXAS", "incyte": "INCY",
        "ionis": "IONS", "neurocrine": "NBIX", "sage": "SAGE",
        "acadia": "ACAD", "madrigal": "MDGL", "krystal": "KRYS",
        "ultragenyx": "RARE", "amicus": "FOLD", "arrowhead": "ARWR",
        "beam": "BEAM", "editas": "EDIT", "intellia": "NTLA",
        "crispr": "CRSP", "bluebird": "BLUE", "blueprint": "BPMC",
        "kymera": "KYMR", "rocket": "RCKT", "praxis": "PRAX",
        "imvax": "IMVT", "denali": "DNLI", "novavax": "NVAX",
        "alkermes": "ALKS", "halozyme": "HALO",
        "iovance": "IOVA", "heron": "HRTX",
        "arvinas": "ARVN", "agios": "AGIO",
    }
    for fragment, ticker in NAME_TO_TICKER.items():
        if fragment in s:
            return ticker
    return None


# ── Main entry point ───────────────────────────────────────────────────────────

async def scan_catalysts() -> List[CatalystEvent]:
    """
    Full catalyst scan — pulls all sources, cross-references stock data,
    deduplicates, sorts by priority and proximity.
    """
    # 1. Pull biotech stock data (we only need quotes, run in thread pool)
    logger.info("Fetching biotech universe stock data...")
    loop = asyncio.get_event_loop()
    stock_cache: Dict[str, Dict] = {}

    async def _fetch_quote(sym: str):
        try:
            q = await loop.run_in_executor(None, get_quote, sym)
            if q:
                stock_cache[sym] = q
        except Exception:
            pass

    await asyncio.gather(*[_fetch_quote(s) for s in BIOTECH_TICKERS])
    logger.info("Got quotes for %d biotech tickers", len(stock_cache))

    # 2. Pull all external catalyst data concurrently
    logger.info("Pulling external catalyst data...")
    catalyst_data = await pull_all_catalyst_data()

    # 3. Parse each source into CatalystEvent objects
    all_events: List[CatalystEvent] = []

    all_events += _parse_fda_approvals(catalyst_data["fda_approvals"], stock_cache)
    all_events += _parse_pdufa_filings(catalyst_data["pdufa_filings"], stock_cache)
    all_events += _parse_drug_filings(catalyst_data["drug_filings"], stock_cache)
    all_events += _parse_phase3_trials(catalyst_data["phase3_trials"], stock_cache)

    # 4. Deduplicate by (symbol, event_type)
    seen: set = set()
    unique_events: List[CatalystEvent] = []
    for ev in all_events:
        key = (ev.symbol, ev.event_type, ev.event_date or "")
        if key not in seen:
            seen.add(key)
            unique_events.append(ev)

    # 5. Sort: HIGH priority first, then by days_until ascending
    def _sort_key(ev: CatalystEvent):
        pri_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        days = ev.days_until if ev.days_until is not None else 9999
        return (pri_order.get(ev.priority, 2), days)

    unique_events.sort(key=_sort_key)
    logger.info("Catalyst scan complete: %d events found", len(unique_events))
    return unique_events
