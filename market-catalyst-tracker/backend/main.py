"""
Market Catalyst Tracker — FastAPI backend
==========================================
All routes serve JSON to the frontend.
WebSocket /ws/prices streams live quote ticks.

Run: uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import INDICES, SECTORS, MOMENTUM_UNIVERSE, BIOTECH_TICKERS
from data.yfinance_adapter import (
    get_quote, get_quotes_bulk, get_history, get_news, get_market_news,
)
from data.finnhub_adapter import get_recommendation_trends, get_peers
from data.fred_adapter import get_latest_macro_indicators, get_yield_curve_spread
from analysis.news_correlator import correlate_market_news, correlate_symbol_news
from analysis.catalyst_scanner import scan_catalysts
from analysis.momentum_scanner import run_momentum_scan, run_squeeze_scan
from models.schemas import (
    StockQuote, Candle, NewsItem, MarketOverview,
    NewsCorrelation, CatalystEvent, MomentumCandidate, ScanResult,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s │ %(name)s │ %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Market Catalyst Tracker",
    description="Real-time market intelligence: catalysts, momentum, news correlation",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_executor = ThreadPoolExecutor(max_workers=16)

# ── Simple TTL cache (avoids hammering APIs repeatedly) ───────────────────────

_cache: Dict[str, tuple] = {}   # key → (data, expires_at)
_SCAN_TTL = 300   # 5 min for heavy scans
_QUOTE_TTL = 30   # 30 sec for quotes
_NEWS_TTL = 120   # 2 min for news


def _cached(key: str, ttl: int, fn, *args, **kwargs):
    """Synchronous cache wrapper."""
    now = time.time()
    if key in _cache:
        data, exp = _cache[key]
        if now < exp:
            return data
    result = fn(*args, **kwargs)
    _cache[key] = (result, now + ttl)
    return result


async def _acached(key: str, ttl: int, coro):
    """Async cache wrapper for coroutines."""
    now = time.time()
    if key in _cache:
        data, exp = _cache[key]
        if now < exp:
            return data
    result = await coro
    _cache[key] = (result, now + ttl)
    return result


# ── Static files (frontend) ───────────────────────────────────────────────────

_FRONTEND = Path(__file__).parent.parent / "frontend"
if _FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND / "static")), name="static")


@app.get("/", include_in_schema=False)
async def serve_index():
    index = _FRONTEND / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"status": "Market Catalyst Tracker API running"})


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": int(time.time())}


# ── Market Overview ────────────────────────────────────────────────────────────

@app.get("/api/market/overview", response_model=MarketOverview)
async def market_overview():
    """Major indices + sector ETFs + top movers."""
    loop = asyncio.get_event_loop()

    all_symbols = list(set(INDICES + SECTORS))

    def _fetch_bulk():
        quotes = []
        for sym in all_symbols:
            q = get_quote(sym)
            if q:
                quotes.append(q)
        return quotes

    raw_quotes = await loop.run_in_executor(_executor, _fetch_bulk)

    index_syms = set(INDICES)
    sector_syms = set(SECTORS)

    indices: List[StockQuote] = []
    sectors: List[StockQuote] = []

    for q in raw_quotes:
        sq = StockQuote(**q)
        if q["symbol"] in index_syms:
            indices.append(sq)
        elif q["symbol"] in sector_syms:
            sectors.append(sq)

    # Top movers from a wider universe (async)
    movers_raw = await loop.run_in_executor(
        _executor,
        lambda: [get_quote(s) for s in MOMENTUM_UNIVERSE[:40] if get_quote(s)],
    )
    movers = [StockQuote(**q) for q in movers_raw if q]
    gainers = sorted(movers, key=lambda x: x.change_pct, reverse=True)[:5]
    losers = sorted(movers, key=lambda x: x.change_pct)[:5]

    return MarketOverview(
        indices=indices,
        sectors=sorted(sectors, key=lambda x: x.change_pct, reverse=True),
        top_gainers=gainers,
        top_losers=losers,
        timestamp=int(time.time()),
    )


@app.get("/api/market/movers")
async def market_movers(limit: int = Query(10, ge=1, le=50)):
    """Top gainers and losers from the momentum universe."""
    loop = asyncio.get_event_loop()
    universe = MOMENTUM_UNIVERSE[:60]

    def _fetch():
        results = []
        for sym in universe:
            q = get_quote(sym)
            if q:
                results.append(q)
        return results

    raw = await loop.run_in_executor(_executor, _fetch)
    raw.sort(key=lambda x: x["change_pct"], reverse=True)
    return {
        "gainers": raw[:limit],
        "losers": raw[-limit:][::-1],
        "timestamp": int(time.time()),
    }


# ── Charts / OHLCV ────────────────────────────────────────────────────────────

@app.get("/api/charts/{symbol}")
async def get_chart_data(
    symbol: str,
    period: str = Query("3mo", regex=r"^(\d+[dmyDMY]|1mo|3mo|6mo|1y|2y|5y|max)$"),
    interval: str = Query("1d", regex=r"^(1m|5m|15m|30m|1h|1d|1wk|1mo)$"),
):
    """OHLCV candle data formatted for TradingView lightweight-charts."""
    loop = asyncio.get_event_loop()
    candles = await loop.run_in_executor(
        _executor, lambda: get_history(symbol.upper(), period, interval)
    )
    if not candles:
        raise HTTPException(404, f"No data found for {symbol}")
    return {"symbol": symbol.upper(), "period": period, "interval": interval, "candles": candles}


@app.get("/api/quote/{symbol}")
async def get_single_quote(symbol: str):
    loop = asyncio.get_event_loop()
    q = await loop.run_in_executor(_executor, lambda: get_quote(symbol.upper()))
    if not q:
        raise HTTPException(404, f"Symbol {symbol} not found")
    return q


# ── News & Correlation ────────────────────────────────────────────────────────

@app.get("/api/news/market", response_model=List[NewsItem])
async def market_news():
    """Broad market news enriched with sentiment and driver category."""
    loop = asyncio.get_event_loop()

    def _fetch():
        raw = get_market_news(limit=30)
        from analysis.news_correlator import _enrich_news_items
        return [n.model_dump() for n in _enrich_news_items(raw)]

    items = await loop.run_in_executor(_executor, _fetch)
    return items


@app.get("/api/news/{symbol}")
async def symbol_news(symbol: str):
    """News for a specific symbol with sentiment scoring."""
    loop = asyncio.get_event_loop()

    def _fetch():
        raw = get_news(symbol.upper(), limit=15)
        from analysis.news_correlator import _enrich_news_items
        return [n.model_dump() for n in _enrich_news_items(raw)]

    items = await loop.run_in_executor(_executor, _fetch)
    return {"symbol": symbol.upper(), "news": items, "timestamp": int(time.time())}


@app.get("/api/news/correlation/market", response_model=NewsCorrelation)
async def market_news_correlation():
    """
    Why is the broad market (SPY) moving right now?
    Returns primary driver, confidence score, and supporting headlines.
    """
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, lambda: correlate_market_news("SPY"))
    return result


@app.get("/api/news/correlation/{symbol}")
async def symbol_news_correlation(symbol: str):
    """Why is this specific stock moving today?"""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor, lambda: correlate_symbol_news(symbol.upper())
    )
    return result


# ── Catalyst Scanner ──────────────────────────────────────────────────────────

@app.get("/api/catalysts/biotech", response_model=List[CatalystEvent])
async def biotech_catalysts(
    priority: Optional[str] = Query(None, regex="^(HIGH|MEDIUM|LOW)$"),
):
    """
    Upcoming biotech / FDA catalysts from:
    - FDA public API (recent approvals/actions)
    - SEC EDGAR 8-K filings mentioning PDUFA
    - ClinicalTrials.gov Phase 3 completions

    Heavy endpoint — cached for 5 minutes.
    """
    events = await _acached("catalysts", _SCAN_TTL, scan_catalysts())
    if priority:
        events = [e for e in events if e.priority == priority]
    return events


@app.get("/api/catalysts/upcoming")
async def upcoming_catalysts(days: int = Query(90, ge=1, le=365)):
    """Events with a known date within the next N days."""
    events = await _acached("catalysts", _SCAN_TTL, scan_catalysts())
    filtered = [
        e for e in events
        if e.days_until is not None and 0 <= e.days_until <= days
    ]
    return {"events": [e.model_dump() for e in filtered], "timestamp": int(time.time())}


# ── Momentum Scanner ─────────────────────────────────────────────────────────

@app.get("/api/momentum/scan", response_model=ScanResult)
async def momentum_scan(
    min_rvol: float = Query(1.5, ge=1.0, le=20.0),
    min_score: float = Query(20.0, ge=0.0, le=100.0),
    top_n: int = Query(30, ge=1, le=100),
):
    """
    Full momentum scan — screens the universe for high-rvol, gap, breakout,
    and short squeeze candidates. Cached 5 minutes.
    """
    cache_key = f"momentum_{min_rvol}_{min_score}_{top_n}"
    result = await _acached(
        cache_key,
        _SCAN_TTL,
        run_momentum_scan(min_rvol=min_rvol, min_score=min_score, top_n=top_n),
    )
    return result


@app.get("/api/momentum/squeeze", response_model=ScanResult)
async def squeeze_scan(top_n: int = Query(20, ge=1, le=50)):
    """Short squeeze candidates — ranked by short interest + positive momentum."""
    result = await _acached("squeeze", _SCAN_TTL, run_squeeze_scan(top_n=top_n))
    return result


# ── Search ────────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search_symbol(q: str = Query(..., min_length=1, max_length=10)):
    """Quick symbol lookup — returns quote if found, 404 otherwise."""
    sym = q.upper().strip()
    loop = asyncio.get_event_loop()
    quote = await loop.run_in_executor(_executor, lambda: get_quote(sym))
    if not quote:
        raise HTTPException(404, f"Symbol '{sym}' not found or no data available")
    return quote


# ── Macro indicators (FRED — optional) ───────────────────────────────────────

@app.get("/api/macro/indicators")
async def macro_indicators():
    """Macro economic indicators via FRED. Returns empty dict if no FRED key."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(_executor, get_latest_macro_indicators)
    spread = await loop.run_in_executor(_executor, get_yield_curve_spread)
    return {"indicators": data, "yield_curve_spread_10y2y": spread, "timestamp": int(time.time())}


# ── WebSocket — live price stream ─────────────────────────────────────────────

class _PriceStreamManager:
    """Manages WebSocket connections and broadcasts price ticks."""

    def __init__(self):
        self.connections: List[WebSocket] = []
        self._running = False

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)
        logger.info("WS connect: %d active", len(self.connections))

    def disconnect(self, ws: WebSocket):
        self.connections.remove(ws)
        logger.info("WS disconnect: %d active", len(self.connections))

    async def broadcast(self, message: Dict):
        payload = json.dumps(message)
        dead = []
        for ws in self.connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.connections:
                self.connections.remove(ws)

    async def start_ticker(self, symbols: List[str], interval: float = 15.0):
        """Background task: fetch quotes periodically and broadcast."""
        if self._running:
            return
        self._running = True
        loop = asyncio.get_event_loop()
        while self._running:
            if self.connections:
                quotes = await loop.run_in_executor(
                    _executor,
                    lambda: [q for q in (get_quote(s) for s in symbols) if q],
                )
                if quotes:
                    await self.broadcast({
                        "type": "quote_update",
                        "quotes": quotes,
                        "timestamp": int(time.time()),
                    })
            await asyncio.sleep(interval)


_stream = _PriceStreamManager()


@app.on_event("startup")
async def startup():
    asyncio.create_task(
        _stream.start_ticker(INDICES + SECTORS[:5], interval=20.0)
    )


@app.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket):
    await _stream.connect(websocket)
    try:
        while True:
            # Keep connection alive; client can send symbol subscription msgs
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
                if data.get("action") == "subscribe":
                    symbols = data.get("symbols", [])
                    if symbols:
                        loop = asyncio.get_event_loop()
                        quotes = await loop.run_in_executor(
                            _executor,
                            lambda: [q for q in (get_quote(s.upper()) for s in symbols[:10]) if q],
                        )
                        await websocket.send_text(json.dumps({
                            "type": "quote_snapshot",
                            "quotes": quotes,
                            "timestamp": int(time.time()),
                        }))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        _stream.disconnect(websocket)
