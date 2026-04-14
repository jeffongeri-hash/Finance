"""
Polymarket adapter — Gamma API + CLOB API.
Both endpoints are completely free, no API key required.

Sources (learned from polymarket-paper-trader/pm_trader/api.py and polyrec/dash.py):
  Gamma API:  https://gamma-api.polymarket.com   — market discovery, metadata, prices
  CLOB API:   https://clob.polymarket.com        — live order books, midpoint prices
  WS CLOB:    wss://ws-subscriptions-clob.polymarket.com/ws/market  (future: live feed)

Key concepts:
  market    → a single YES/NO binary question with two tokens (Yes token, No token)
  event     → a group of related markets (e.g. "2024 Presidential Election")
  outcome   → "Yes" or "No" (outcome_price = implied probability 0→1)
  token_id  → CLOB identifier for a specific outcome token
  volume    → total $ traded on this market
  liquidity → current $ available in the order book
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from config import HTTP_TIMEOUT

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

# Tags on Polymarket that contain markets relevant to our use cases
RELEVANT_TAG_SLUGS = {
    "science",      # biotech, drug approvals, clinical trials
    "health",       # FDA, pharma, public health
    "economics",    # Fed rates, CPI, recession, GDP
    "politics",     # tariffs, trade deals, elections (macro drivers)
    "crypto",       # BTC, ETH (risk-on/off signal)
}

# ── Internal request helpers ──────────────────────────────────────────────────

async def _gamma_get(path: str, params: Optional[Dict] = None) -> Any:
    url = f"{GAMMA_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
            r = await c.get(url, params=params or {})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning("Gamma GET %s: %s", path, e)
        return []


async def _clob_get(path: str, params: Optional[Dict] = None) -> Any:
    url = f"{CLOB_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
            r = await c.get(url, params=params or {})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning("CLOB GET %s: %s", path, e)
        return {}


# ── Market parsing (mirrors pm_trader/api.py _parse_market) ──────────────────

def _parse_market(raw: Dict) -> Optional[Dict]:
    """Normalise a Gamma API market dict into our flat schema."""
    cid = raw.get("conditionId") or raw.get("condition_id")
    if not cid:
        return None

    # outcome prices: JSON string or list of floats → [yes_prob, no_prob]
    op_raw = raw.get("outcomePrices") or raw.get("outcome_prices", "[]")
    if isinstance(op_raw, str):
        try:
            op_raw = json.loads(op_raw)
        except Exception:
            op_raw = []
    outcome_prices = [float(p) for p in op_raw] if op_raw else []

    # outcomes
    out_raw = raw.get("outcomes", '["Yes","No"]')
    if isinstance(out_raw, str):
        try:
            out_raw = json.loads(out_raw)
        except Exception:
            out_raw = ["Yes", "No"]

    # token ids (for CLOB order book lookups)
    tok_raw = raw.get("clobTokenIds", "[]")
    if isinstance(tok_raw, str):
        try:
            tok_raw = json.loads(tok_raw)
        except Exception:
            tok_raw = []

    yes_price = outcome_prices[0] if outcome_prices else None
    no_price  = outcome_prices[1] if len(outcome_prices) > 1 else None
    yes_token = tok_raw[0] if tok_raw else None

    tags = raw.get("tags") or []
    tag_slugs = []
    for t in tags:
        if isinstance(t, dict):
            tag_slugs.append(t.get("slug", ""))
        elif isinstance(t, str):
            tag_slugs.append(t)

    return {
        "condition_id": cid,
        "slug":         raw.get("slug", ""),
        "question":     raw.get("question", ""),
        "description":  (raw.get("description") or "")[:300],
        "outcomes":     out_raw,
        "yes_price":    round(yes_price, 4) if yes_price is not None else None,
        "no_price":     round(no_price,  4) if no_price  is not None else None,
        "yes_token_id": yes_token,
        "volume":       float(raw.get("volume")    or 0),
        "liquidity":    float(raw.get("liquidity") or 0),
        "active":       bool(raw.get("active", True)),
        "closed":       bool(raw.get("closed", False)),
        "end_date":     raw.get("endDateIso") or raw.get("end_date_iso") or raw.get("endDate") or "",
        "tags":         tag_slugs,
        "url":          f"https://polymarket.com/event/{raw.get('slug','')}" if raw.get("slug") else "",
    }


# ── Public query functions ────────────────────────────────────────────────────

async def get_top_markets(limit: int = 50, closed: bool = False) -> List[Dict]:
    """Top active markets sorted by volume."""
    raw = await _gamma_get("/markets", {
        "limit":     limit,
        "active":    "true",
        "closed":    str(closed).lower(),
        "order":     "volume",
        "ascending": "false",
    })
    if not isinstance(raw, list):
        return []
    return [m for m in (_parse_market(r) for r in raw) if m]


async def search_markets(query: str, limit: int = 10) -> List[Dict]:
    """Full-text search across Polymarket market questions."""
    raw = await _gamma_get("/markets", {"_q": query, "limit": limit, "active": "true"})
    if not isinstance(raw, list):
        return []
    return [m for m in (_parse_market(r) for r in raw) if m]


async def get_markets_by_tag(tag_slug: str, limit: int = 30, closed: bool = False) -> List[Dict]:
    """
    Markets filtered by a Polymarket tag slug.
    Relevant slugs: 'science', 'health', 'economics', 'politics', 'crypto'
    """
    raw = await _gamma_get("/markets", {
        "tag_slug": tag_slug,
        "limit":    limit,
        "active":   str(not closed).lower(),
        "closed":   str(closed).lower(),
        "order":    "volume",
        "ascending":"false",
    })
    if not isinstance(raw, list):
        return []
    return [m for m in (_parse_market(r) for r in raw) if m]


async def get_available_tags() -> List[Dict]:
    """All Polymarket tags (slug + label)."""
    raw = await _gamma_get("/tags")
    if not isinstance(raw, list):
        return []
    return [{"slug": t.get("slug",""), "label": t.get("label", t.get("slug",""))} for t in raw if t.get("slug")]


async def get_live_midpoint(token_id: str) -> Optional[float]:
    """
    Live YES probability from CLOB midpoint.
    Returns float 0.0–1.0 or None on failure.
    """
    if not token_id:
        return None
    data = await _clob_get("/midpoint", {"token_id": token_id})
    try:
        return float(data.get("mid", 0)) or None
    except (TypeError, ValueError):
        return None


async def get_order_book(token_id: str) -> Dict:
    """
    Live order book from CLOB for a YES token.
    Returns {"bids": [...], "asks": [...]} with price/size dicts.
    """
    if not token_id:
        return {"bids": [], "asks": []}
    raw = await _clob_get("/book", {"token_id": token_id})
    bids = [{"price": float(b["price"]), "size": float(b["size"])} for b in raw.get("bids", [])]
    asks = [{"price": float(a["price"]), "size": float(a["size"])} for a in raw.get("asks", [])]
    return {"bids": sorted(bids, key=lambda x: -x["price"])[:10],
            "asks": sorted(asks, key=lambda x:  x["price"])[:10]}


async def enrich_with_live_price(market: Dict) -> Dict:
    """
    Replace Gamma's cached outcome_prices with live CLOB midpoint.
    Mutates and returns the market dict.
    """
    tok = market.get("yes_token_id")
    if tok:
        live = await get_live_midpoint(tok)
        if live is not None:
            market["yes_price"] = round(live, 4)
            market["no_price"]  = round(1 - live, 4)
            market["price_source"] = "clob_live"
        else:
            market["price_source"] = "gamma_cached"
    return market


async def get_price_history(token_id: str, interval: str = "1d", fidelity: int = 1) -> List[Dict]:
    """
    Historical price (probability) series for a token — useful for charting.
    interval: '1m','5m','1h','6h','1d'
    Returns list of {t: unix_ts, p: probability}
    """
    raw = await _gamma_get("/prices-history", {
        "market":   token_id,
        "interval": interval,
        "fidelity": fidelity,
    })
    if not isinstance(raw, dict):
        return []
    history = raw.get("history") or []
    return [{"t": int(h.get("t", 0)), "p": float(h.get("p", 0))} for h in history]
