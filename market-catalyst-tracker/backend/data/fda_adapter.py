"""
FDA & biotech catalyst data — all sources are completely free, no API key.

Sources:
1. FDA public API (api.fda.gov)          — recent drug approval actions
2. SEC EDGAR full-text search            — 8-K filings mentioning PDUFA
3. ClinicalTrials.gov API v2             — Phase 3 trials near completion
"""
from __future__ import annotations
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import (
    FDA_API_BASE,
    EDGAR_SEARCH_URL,
    CLINICAL_TRIALS_API,
    HTTP_TIMEOUT,
)

logger = logging.getLogger(__name__)

# ── FDA API ────────────────────────────────────────────────────────────────────

async def get_recent_fda_approvals(limit: int = 25) -> List[Dict]:
    """
    Pull the most recent NDA/BLA approval actions from FDA's public drug DB.
    Returns a list of raw result dicts.
    """
    params = {
        "search": 'submissions.submission_status:"AP"',
        "limit": limit,
        "sort": "submissions.submission_status_date:desc",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(f"{FDA_API_BASE}/drugsfda.json", params=params)
            if r.status_code == 200:
                return r.json().get("results", [])
            logger.warning("FDA API returned %s", r.status_code)
    except Exception as e:
        logger.warning("FDA API error: %s", e)
    return []


def _extract_pdufa_date(text: str) -> Optional[str]:
    """Try to extract a PDUFA date from filing text using common patterns."""
    patterns = [
        r"PDUFA\s+(?:date|deadline|action\s+date)\s+(?:is|of|:)?\s*([A-Z][a-z]+ \d{1,2},? \d{4})",
        r"([A-Z][a-z]+ \d{1,2},? \d{4})\s+PDUFA",
        r"PDUFA\W+(\d{1,2}/\d{1,2}/\d{4})",
        r"(\d{4}-\d{2}-\d{2})\s+PDUFA",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


# ── SEC EDGAR ─────────────────────────────────────────────────────────────────

async def search_edgar_pdufa_filings(days_back: int = 90, limit: int = 40) -> List[Dict]:
    """
    Full-text search EDGAR for 8-K filings mentioning PDUFA.
    Returns parsed hits with ticker, headline, date, and extracted PDUFA date.
    """
    start_dt = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    params = {
        "q": '"PDUFA"',
        "forms": "8-K",
        "dateRange": "custom",
        "startdt": start_dt,
        "hits.hits._source": "period_of_report,entity_name,file_date,form_type,display_names",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(EDGAR_SEARCH_URL, params=params)
            if r.status_code != 200:
                return []
            hits = r.json().get("hits", {}).get("hits", [])
            results = []
            for h in hits[:limit]:
                src = h.get("_source", {})
                names = src.get("display_names", [])
                # display_names is list of "Name (TICKER)"
                tickers = []
                for dn in names:
                    m = re.search(r"\(([A-Z]{1,5})\)", dn)
                    if m:
                        tickers.append(m.group(1))
                results.append({
                    "tickers": tickers,
                    "entity": src.get("entity_name", ""),
                    "file_date": src.get("period_of_report") or src.get("file_date", ""),
                    "form": src.get("form_type", "8-K"),
                    "accession": h.get("_id", ""),
                    "pdufa_date_text": None,  # would need to fetch full filing
                    "source_url": (
                        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                        f"&company={src.get('entity_name','')}&type=8-K&dateb=&owner=include&count=10"
                    ),
                })
            return results
    except Exception as e:
        logger.warning("EDGAR search error: %s", e)
    return []


async def search_edgar_drug_approvals(days_back: int = 30) -> List[Dict]:
    """Search for 8-K filings about FDA approvals/rejections."""
    start_dt = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    params = {
        "q": '"FDA" "approval" OR "Complete Response Letter" OR "CRL"',
        "forms": "8-K",
        "dateRange": "custom",
        "startdt": start_dt,
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(EDGAR_SEARCH_URL, params=params)
            if r.status_code != 200:
                return []
            hits = r.json().get("hits", {}).get("hits", [])
            results = []
            for h in hits[:30]:
                src = h.get("_source", {})
                names = src.get("display_names", [])
                tickers = []
                for dn in names:
                    m = re.search(r"\(([A-Z]{1,5})\)", dn)
                    if m:
                        tickers.append(m.group(1))
                results.append({
                    "tickers": tickers,
                    "entity": src.get("entity_name", ""),
                    "file_date": src.get("period_of_report") or src.get("file_date", ""),
                    "headline": f"FDA-related 8-K: {src.get('entity_name', '')}",
                    "source_url": (
                        "https://efts.sec.gov/LATEST/search-index?q=%22FDA%22+%22approval%22"
                        "&forms=8-K&dateRange=custom&startdt=" + start_dt
                    ),
                })
            return results
    except Exception as e:
        logger.warning("EDGAR drug approval search error: %s", e)
    return []


# ── ClinicalTrials.gov ────────────────────────────────────────────────────────

async def get_phase3_trials(
    condition: str = "oncology",
    status: str = "ACTIVE_NOT_RECRUITING",
    limit: int = 20,
) -> List[Dict]:
    """
    Phase 3 trials that are active but not recruiting = close to primary endpoint read-out.
    condition: e.g. 'oncology', 'cardiovascular', 'rare disease'
    """
    params = {
        "query.cond": condition,
        "filter.overallStatus": status,
        "filter.phase": "PHASE3",
        "pageSize": limit,
        "fields": "NCTId,BriefTitle,OfficialTitle,OverallStatus,Phase,LeadSponsorName,"
                  "StartDate,PrimaryCompletionDate,CompletionDate,BriefSummary",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(CLINICAL_TRIALS_API, params=params)
            if r.status_code == 200:
                data = r.json()
                return data.get("studies", [])
    except Exception as e:
        logger.warning("ClinicalTrials.gov error: %s", e)
    return []


async def get_phase3_oncology_trials() -> List[Dict]:
    return await get_phase3_trials("oncology OR cancer")


async def get_phase3_rare_disease_trials() -> List[Dict]:
    return await get_phase3_trials("rare disease OR orphan")


# ── Aggregated catalyst pull ──────────────────────────────────────────────────

async def pull_all_catalyst_data() -> Dict[str, Any]:
    """
    Master pull: runs all sources concurrently.
    Returns a dict with keys: fda_approvals, pdufa_filings, drug_filings, phase3_trials
    """
    import asyncio
    fda_task = asyncio.create_task(get_recent_fda_approvals())
    pdufa_task = asyncio.create_task(search_edgar_pdufa_filings())
    drug_task = asyncio.create_task(search_edgar_drug_approvals())
    onco_task = asyncio.create_task(get_phase3_oncology_trials())
    rare_task = asyncio.create_task(get_phase3_rare_disease_trials())

    fda_approvals, pdufa_filings, drug_filings, onco_trials, rare_trials = await asyncio.gather(
        fda_task, pdufa_task, drug_task, onco_task, rare_task,
        return_exceptions=True,
    )

    return {
        "fda_approvals": fda_approvals if isinstance(fda_approvals, list) else [],
        "pdufa_filings": pdufa_filings if isinstance(pdufa_filings, list) else [],
        "drug_filings": drug_filings if isinstance(drug_filings, list) else [],
        "phase3_trials": (
            (onco_trials if isinstance(onco_trials, list) else [])
            + (rare_trials if isinstance(rare_trials, list) else [])
        ),
    }
