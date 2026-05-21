"""
Market Catalyst Tracker — FastAPI backend
==========================================
All routes serve JSON to the frontend.
WebSocket /ws/prices streams live quote ticks.

Run: uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations
import asyncio
import importlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import (
    INDICES, SECTORS, MOMENTUM_UNIVERSE, BIOTECH_TICKERS,
    POLY_PRIVATE_KEY, POLY_FUNDER, POLY_HOST,
)
from data.nasdaq_adapter import (
    get_short_interest, get_macro_snapshot as nasdaq_macro_snapshot,
    get_ohlcv_history as nasdaq_ohlcv, get_status as nasdaq_status,
)
from data.yfinance_adapter import (
    get_quote, get_quotes_bulk, get_history, get_news, get_market_news,
)
from data.finnhub_adapter import get_recommendation_trends, get_peers
from data.fred_adapter import get_latest_macro_indicators, get_yield_curve_spread
from analysis.news_correlator import correlate_market_news, correlate_symbol_news
from analysis.catalyst_scanner import scan_catalysts
from analysis.momentum_scanner import run_momentum_scan, run_squeeze_scan
from trading.engine import create_engine, get_engine
from trading.signals import run_signal_scan
from trading.backtester import (
    create_backtest_runner, get_backtest_runner,
    run_full_backtest, backtest_market, aggregate_results,
    STRATEGY_REGISTRY,
)
from trading.penny_scanner import (
    scan_penny_markets, rank_opportunities,
    calc_ev, calc_confidence, validate_portfolio_ev,
    ENTRY_PRICE as PENNY_ENTRY, TAKE_PROFIT as PENNY_TP,
    TARGET_POS as PENNY_TARGET, ORDER_SIZE as PENNY_SIZE,
    FULL_RESOLVE_RATE, BOUNCE_RATE, LOSS_RATE,
)
from analysis.technical import get_mtf_analysis
from analysis.volatility import get_volatility_profile, get_trade_setup
from analysis.prediction_scanner import (
    pull_all_prediction_data,
    get_biotech_fda_markets,
    get_macro_markets,
    get_geopolitical_markets,
    get_top_markets_all,
    enrich_catalysts_with_predictions,
    get_market_chart_data,
)
from equity.hedge_fund_bridge import (
    equity_status as _equity_status,
    list_agents as _list_agents,
    run_analysis as _run_equity_analysis,
)
from data.polymarket_adapter import (
    search_markets as pm_search,
    get_order_book as pm_order_book,
    get_live_midpoint,
    enrich_with_live_price,
)
from models.schemas import (
    StockQuote, Candle, NewsItem, MarketOverview,
    NewsCorrelation, CatalystEvent, MomentumCandidate, ScanResult,
    PredictionMarket, EnrichedCatalyst, PredictionSnapshot,
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

_executor = ThreadPoolExecutor(max_workers=4)   # reduced for 512MB Railway container

# ── Simple TTL cache (avoids hammering APIs repeatedly) ───────────────────────

_cache: Dict[str, tuple] = {}   # key → (data, expires_at)
_SCAN_TTL = 600   # 10 min for heavy scans
_QUOTE_TTL = 120  # 2 min for quotes (yfinance is the bottleneck on Railway)
_NEWS_TTL = 300   # 5 min for news


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
    period: str = Query("3mo", pattern=r"^(\d+[dmyDMY]|1mo|3mo|6mo|1y|2y|5y|max)$"),
    interval: str = Query("1d", pattern=r"^(1m|5m|15m|30m|1h|1d|1wk|1mo)$"),
):
    """OHLCV candle data formatted for TradingView lightweight-charts."""
    loop = asyncio.get_event_loop()
    candles = await loop.run_in_executor(
        _executor, lambda: get_history(symbol.upper(), period, interval)
    )
    if not candles:
        return {"symbol": symbol.upper(), "period": period, "interval": interval, "candles": [], "warning": "No data available — market may be closed or symbol invalid"}
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
    priority: Optional[str] = Query(None, pattern="^(HIGH|MEDIUM|LOW)$"),
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
    min_rvol: float = Query(1.2, ge=1.0, le=20.0),
    min_score: float = Query(10.0, ge=0.0, le=100.0),
    top_n: int = Query(30, ge=1, le=100),
):
    """
    Full momentum scan — screens the universe for high-rvol, gap, breakout,
    and short squeeze candidates. Cached 10 minutes.

    Thresholds lowered (min_rvol 1.5→1.2, min_score 20→10) because the scanner
    now uses fast_info only (no t.info short-interest data), so raw scores are
    lower on average. This still filters out flat/low-activity stocks.
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


# ── Prediction Markets (Polymarket) ──────────────────────────────────────────

@app.get("/api/predictions/top")
async def prediction_top(limit: int = Query(50, ge=1, le=100)):
    """
    Top Polymarket markets by volume — broad view across all categories.
    Includes category label (biotech_fda | macro | geopolitical | crypto | other).
    Cached 5 minutes.
    """
    markets = await _acached("pred_top", _SCAN_TTL, get_top_markets_all(limit))
    return {"markets": markets, "count": len(markets), "timestamp": int(time.time())}


@app.get("/api/predictions/biotech")
async def prediction_biotech(limit: int = Query(30, ge=1, le=100)):
    """
    Polymarket markets about FDA drug approvals, clinical trials, biotech.
    These are the crowd-wisdom counterpart to the catalyst calendar —
    e.g. 'Will Moderna's drug get FDA approval by Q2?' → yes_price = 0.71.
    Cached 5 minutes.
    """
    markets = await _acached("pred_bio", _SCAN_TTL, get_biotech_fda_markets(limit))
    return {"markets": markets, "count": len(markets), "timestamp": int(time.time())}


@app.get("/api/predictions/macro")
async def prediction_macro(limit: int = Query(30, ge=1, le=100)):
    """
    Polymarket macro-economic markets:
    Fed rate decisions, CPI outcomes, recession probability, tariffs.
    Useful alongside the 'Why is the market moving?' correlator.
    Cached 5 minutes.
    """
    markets = await _acached("pred_macro", _SCAN_TTL, get_macro_markets(limit))
    return {"markets": markets, "count": len(markets), "timestamp": int(time.time())}


@app.get("/api/predictions/geopolitical")
async def prediction_geopolitical(limit: int = Query(20, ge=1, le=50)):
    """Polymarket geopolitical / political event markets."""
    markets = await _acached("pred_geo", _SCAN_TTL, get_geopolitical_markets(limit))
    return {"markets": markets, "count": len(markets), "timestamp": int(time.time())}


@app.get("/api/predictions/search")
async def prediction_search(q: str = Query(..., min_length=2, max_length=100)):
    """Full-text search across Polymarket markets."""
    results = await pm_search(q.strip(), limit=15)
    # Enrich top-3 with live CLOB prices
    for m in results[:3]:
        await enrich_with_live_price(m)
    return {"query": q, "results": results, "count": len(results)}


@app.get("/api/predictions/snapshot")
async def prediction_snapshot():
    """
    Full snapshot: top + biotech + macro + geopolitical in one call.
    Useful for initial page load. Cached 5 minutes.
    """
    data = await _acached("pred_snapshot", _SCAN_TTL, pull_all_prediction_data())
    return data


@app.get("/api/predictions/market/{condition_id}/book")
async def prediction_order_book(condition_id: str):
    """Live order book depth for a specific Polymarket YES token."""
    book = await pm_order_book(condition_id)
    return book


@app.get("/api/predictions/market/{token_id}/history")
async def prediction_price_history(token_id: str):
    """Historical probability series for charting a Polymarket market."""
    history = await get_market_chart_data(token_id)
    return {"token_id": token_id, "history": history}


@app.get("/api/catalysts/biotech/enriched")
async def biotech_catalysts_enriched(
    priority: Optional[str] = Query(None, pattern="^(HIGH|MEDIUM|LOW)$"),
    limit: int = Query(20, ge=1, le=50),
):
    """
    Catalyst calendar with Polymarket prediction market odds attached.
    Each event includes prediction_market: {yes_price, volume, question, url} if found.
    Heavy endpoint — catalyst scan + N Polymarket searches. Cached 5 minutes.
    """
    raw_events = await _acached("catalysts", _SCAN_TTL, scan_catalysts())
    if priority:
        raw_events = [e for e in raw_events if e.priority == priority]
    enriched = await _acached(
        f"enriched_catalysts_{priority}_{limit}",
        _SCAN_TTL,
        enrich_catalysts_with_predictions(raw_events[:limit]),
    )
    return {"events": enriched, "count": len(enriched), "timestamp": int(time.time())}


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
    # Create trading engine — auto-enables live mode if credentials are set in .env
    create_engine(
        initial_balance = 10_000.0,
        private_key     = POLY_PRIVATE_KEY,
        funder          = POLY_FUNDER,
        poly_host       = POLY_HOST,
        live_mode       = bool(POLY_PRIVATE_KEY and POLY_FUNDER),
    )
    # Start continuous backtester — runs every 30 min, results available immediately
    runner = create_backtest_runner(interval_s=1800)
    await runner.start()


# ── Trading Engine Routes ─────────────────────────────────────────────────────

@app.post("/api/trading/start")
async def trading_start(balance: float = 10_000.0):
    """
    Start the automated paper trading engine.
    Begins scanning for signals every 60 seconds and auto-executes trades.
    All trades are paper (simulated) by default.
    """
    engine = get_engine() or create_engine(initial_balance=balance)
    if not engine._running:
        await engine.start()
    return {"status": "running", "balance": engine.trader.balance, "mode": "paper"}


@app.post("/api/trading/stop")
async def trading_stop():
    """Stop the trading engine."""
    engine = get_engine()
    if engine and engine._running:
        await engine.stop()
    return {"status": "stopped"}


@app.get("/api/trading/stats")
async def trading_stats():
    """Real-time P&L, win rate, open positions, trade log, and current signals."""
    engine = get_engine()
    if not engine:
        return {"error": "Engine not initialised. POST /api/trading/start first."}
    return engine.get_stats()


@app.get("/api/trading/signals")
async def trading_signals():
    """
    Run the full signal scan on demand and return current opportunities.
    Covers: order-book imbalance, macro arbitrage, biotech catalyst edge,
            news lag, momentum correlation.
    """
    from analysis.prediction_scanner import get_biotech_fda_markets, get_macro_markets
    from analysis.news_correlator import correlate_market_news
    from analysis.momentum_scanner import run_momentum_scan

    biotech  = await get_biotech_fda_markets(20)
    macro    = await get_macro_markets(20)
    corr     = await asyncio.get_event_loop().run_in_executor(
        _executor, lambda: correlate_market_news("SPY")
    )
    news     = [n.model_dump() for n in corr.supporting_news[:10]]
    momentum = await run_momentum_scan(top_n=20, min_score=35)
    cands    = [c.model_dump() for c in momentum.candidates]

    signals = await run_signal_scan(
        biotech_markets     = biotech,
        macro_markets       = macro,
        news_items          = news,
        momentum_candidates = cands,
    )
    return {"signals": signals, "count": len(signals), "timestamp": int(time.time())}


@app.get("/api/trading/log")
async def trading_log():
    """Engine event log (last 50 entries)."""
    engine = get_engine()
    if not engine:
        return {"log": []}
    return {"log": engine.get_log()[-50:], "timestamp": int(time.time())}


# ── Live Trading Routes ───────────────────────────────────────────────────────

@app.get("/api/trading/live/status")
async def live_status():
    """
    CLOB connection status and live mode health.
    Returns whether live mode is active, USDC balance, and pending order count.
    """
    engine = get_engine()
    if not engine:
        return {"live_mode": False, "note": "Engine not initialised"}
    health = await engine.live_health_check()
    return {
        "live_mode":       engine.live_mode,
        "live_available":  engine._live is not None,
        "live_error":      engine._live_error,
        "clob":            health,
        "timestamp":       int(time.time()),
    }


@app.post("/api/trading/live/enable")
async def live_enable(
    private_key: str = Query(..., description="Polygon wallet private key (0x…)"),
    funder:      str = Query(..., description="Polymarket funder address (0x…)"),
    host:        str = Query("https://clob.polymarket.com", description="CLOB host"),
):
    """
    Switch the active trader to LiveTrader (real CLOB orders, real USDC).

    ⚠ WARNING: After enabling live mode, all BUY/SELL calls will place real orders
    on Polygon mainnet and spend real USDC. Verify position sizes and risk controls
    before enabling.

    The paper trader continues running as a shadow for comparison.
    """
    engine = get_engine()
    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialised. POST /api/trading/start first.")

    result = engine.enable_live_mode(
        private_key=private_key,
        funder=funder,
        host=host,
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])

    # Fetch initial balance from the chain
    if engine._live:
        await engine._live.refresh_balance()

    return {
        "live_mode":     engine.live_mode,
        "balance_usdc":  engine._live.balance if engine._live else 0,
        "funder_prefix": result.get("funder_prefix", funder[:10]),
        "message":       "Live trading ENABLED — real USDC orders are now active",
        "timestamp":     int(time.time()),
    }


@app.post("/api/trading/live/disable")
async def live_disable():
    """
    Switch back to paper trading. Any open live orders remain open on the CLOB
    and must be managed separately (or cancelled via /api/trading/live/cancel-all).
    """
    engine = get_engine()
    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialised")
    result = engine.disable_live_mode()
    return {
        **result,
        "live_mode": engine.live_mode,
        "message":   "Switched back to paper trading",
        "timestamp": int(time.time()),
    }


@app.get("/api/trading/live/balance")
async def live_balance():
    """Fetch real-time USDC balance from the Polymarket CLOB."""
    engine = get_engine()
    if not engine or not engine._live:
        return {"balance_usdc": None, "note": "Live trader not initialised"}
    usdc = await engine._live.refresh_balance()
    return {
        "balance_usdc":   round(usdc, 4),
        "paper_balance":  round(engine._paper.balance, 2),
        "timestamp":      int(time.time()),
    }


@app.get("/api/trading/live/orders")
async def live_orders():
    """Pending (unconfirmed) orders sitting on the Polymarket CLOB."""
    engine = get_engine()
    if not engine or not engine._live:
        return {"orders": [], "count": 0, "note": "Live trader not initialised"}
    orders = engine._live.get_pending_orders()
    return {
        "orders":    orders,
        "count":     len(orders),
        "timestamp": int(time.time()),
    }


@app.post("/api/trading/live/cancel-all")
async def live_cancel_all():
    """
    Cancel all pending GTC orders on the CLOB and refund reserved balance.
    Use this before switching to paper mode if you want a clean slate.
    """
    engine = get_engine()
    if not engine or not engine._live:
        raise HTTPException(status_code=503, detail="Live trader not initialised")
    n = await engine._live.cancel_all_orders()
    return {
        "cancelled": n,
        "message":   f"Cancelled {n} pending order(s)",
        "timestamp": int(time.time()),
    }


# ── Backtester Routes ─────────────────────────────────────────────────────────

@app.get("/api/backtest/status")
async def backtest_status():
    """Current state of the continuous backtester loop."""
    runner = get_backtest_runner()
    if not runner:
        return {"running": False, "note": "Backtester not initialized"}
    return runner.get_status()


@app.get("/api/backtest/summary")
async def backtest_summary():
    """
    Aggregated strategy performance across all replayed markets.
    Returns avg Sharpe, avg return, win rate, expected value per strategy.
    Updated every 30 minutes automatically.
    """
    runner = get_backtest_runner()
    if not runner:
        return {"error": "Backtester not initialized"}
    summary = runner.get_summary()
    if not summary.get("by_strategy"):
        return {
            "note": "Backtest still running — check back in a moment",
            "status": runner.get_status(),
        }
    return summary


@app.get("/api/backtest/results")
async def backtest_results(
    strategy: Optional[str] = Query(None, description="Filter by strategy name"),
    limit: int = Query(50, ge=1, le=500),
):
    """
    Raw per-market backtest results sorted by total return.
    Optional ?strategy= filter to see one strategy across all markets.
    """
    runner = get_backtest_runner()
    if not runner:
        return {"results": [], "count": 0}
    rows = runner.get_results()
    if strategy:
        rows = [r for r in rows if r["strategy"] == strategy]
    rows = sorted(rows, key=lambda r: r["total_return"], reverse=True)[:limit]
    return {"results": rows, "count": len(rows), "strategies": list(STRATEGY_REGISTRY.keys())}


@app.post("/api/backtest/run")
async def backtest_run_now():
    """
    Trigger an immediate backtest sweep (non-blocking — runs in background).
    """
    runner = get_backtest_runner()
    if not runner:
        raise HTTPException(status_code=503, detail="Backtester not initialized")
    asyncio.create_task(runner._run_once())
    return {"triggered": True, "message": "Backtest sweep started — poll /api/backtest/status"}


@app.get("/api/backtest/market/{token_id}")
async def backtest_single_market(
    token_id: str,
    strategy: str = Query("mean_reversion"),
    slug: str = Query("unknown"),
):
    """
    Run a single strategy against a single market on-demand.
    token_id: Polymarket YES token ID (from /api/predictions/*)
    strategy: one of mean_reversion | breakout | panic_fade | threshold_momentum |
              ema_crossover | vwap_reversion | deep_value
    """
    if strategy not in STRATEGY_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown strategy. Valid: {list(STRATEGY_REGISTRY.keys())}"
        )
    result = await backtest_market(slug, token_id, strategy_name=strategy)
    if not result:
        return {
            "error": "Insufficient price history (need ≥30 bars)",
            "token_id": token_id,
            "strategy": strategy,
        }
    return {
        "strategy":      result.strategy,
        "market_slug":   result.market_slug,
        "bars_replayed": result.bars_replayed,
        "trades":        result.trades,
        "win_rate":      result.win_rate,
        "total_return":  result.total_return,
        "sharpe":        result.sharpe,
        "max_drawdown":  result.max_drawdown,
        "avg_duration":  result.avg_duration,
        "profit_factor": result.profit_factor,
        "expected_value":result.expected_value,
        "config":        result.config,
    }


# ── Penny Harvest Routes ─────────────────────────────────────────────────────

@app.get("/api/penny/scan")
async def penny_scan(
    entry_max: float = Query(0.03, description="Max entry price (default 3c)"),
    min_liquidity: float = Query(1000, description="Min market liquidity ($)"),
    min_days: int = Query(14, description="Min days to expiry"),
    max_days: int = Query(200, description="Max days to expiry"),
):
    """
    Scan all active Polymarket markets for 1-cent contracts with positive EV.

    Strategy from @paonx_eth (400M trades / 6 years):
      EV per $0.01 contract = +$0.0336
      Portfolio EV on 50 positions = +206% expected return per cycle

    Returns opportunities sorted by composite score (EV + liquidity + days-to-expiry).
    """
    opps = await _acached(
        f"penny_scan_{entry_max}_{min_liquidity}",
        300,   # 5-min cache
        scan_penny_markets(
            entry_max=entry_max,
            min_liquidity=min_liquidity,
            min_days=min_days,
            max_days=max_days,
        )
    )
    ranked = rank_opportunities(opps) if opps else []
    return {
        "opportunities": ranked,
        "count":         len(ranked),
        "ev_formula":    "EV = (0.0266×$0.99) + (0.0333×$0.50) + (0.94×-entry_price)",
        "portfolio_ev":  validate_portfolio_ev(ranked[:50]),
        "parameters": {
            "entry_max":     entry_max,
            "take_profit":   PENNY_TP,
            "order_size":    PENNY_SIZE,
            "target_pos":    PENNY_TARGET,
            "min_liquidity": min_liquidity,
        },
        "dataset_stats": {
            "full_resolve_rate": FULL_RESOLVE_RATE,
            "bounce_rate":       BOUNCE_RATE,
            "loss_rate":         round(LOSS_RATE, 4),
            "ev_per_contract":   round(calc_ev(PENNY_ENTRY), 4),
        },
        "timestamp": int(time.time()),
    }


@app.get("/api/penny/ev")
async def penny_ev_calculator(price: float = Query(0.01, ge=0.001, le=0.10)):
    """
    Calculate expected value for a given entry price.
    EV = P(full_resolve)×(0.99-p) + P(bounce)×(0.50-p) + P(loss)×(-p)
    """
    ev         = calc_ev(price)
    confidence = calc_confidence(price)
    portfolio_ev_100 = ev * 100 * (1.0 / price)   # 100 positions × 1/price shares
    return {
        "entry_price":    price,
        "ev_per_share":   round(ev, 6),
        "ev_per_dollar":  round(ev / price, 4),
        "confidence":     confidence,
        "portfolio_ev_100_positions": round(portfolio_ev_100, 2),
        "is_positive_ev": ev > 0,
        "breakdown": {
            "full_resolve_contribution": round(FULL_RESOLVE_RATE * (0.99 - price), 6),
            "bounce_contribution":       round(BOUNCE_RATE * (0.50 - price), 6),
            "loss_contribution":         round(LOSS_RATE * (-price), 6),
        },
    }


@app.get("/api/penny/positions")
async def penny_positions():
    """Current open penny harvest positions tracked by the engine."""
    engine = get_engine()
    if not engine:
        return {"positions": [], "count": 0, "target": PENNY_TARGET}
    pos = engine.get_penny_positions()
    return {
        "positions":  pos,
        "count":      len(pos),
        "target":     PENNY_TARGET,
        "portfolio_ev": validate_portfolio_ev(pos),
        "timestamp":  int(time.time()),
    }


# ── Technical Analysis Routes ─────────────────────────────────────────────────

@app.get("/api/analysis/technical/{ticker}")
async def analysis_technical(ticker: str):
    """
    Multi-timeframe technical analysis for any equity or crypto ticker.

    Stacks on three timeframes:
      • 1Y Daily   — SMA20/50/200, EMA9/21, OBV, swing S/R, daily pivots, ATR14
      • 3M Hourly  — EMA9/21/50, OBV, S/R, ATR14
      • 24H Minute — EMA9, VWAP, volume trend, ATR14

    Returns:
      candle data + MA overlay + volume bars for each timeframe (for charting)
      confluence_score [-6, +6] and direction (STRONG_LONG / LONG / NEUTRAL / SHORT / STRONG_SHORT)

    Results cached 5 minutes (heavy multi-timeframe API call).
    """
    key = f"mtf_{ticker.upper()}"
    return await _acached(key, 300, get_mtf_analysis(ticker))


@app.get("/api/analysis/volatility/{ticker}")
async def analysis_volatility(ticker: str):
    """
    Implied volatility profile for equity or crypto.

    For equities: extracts ATM IV from the nearest front-month options chain.
    For crypto (BTC-USD, ETH-USD…): uses 30-day historical volatility as proxy.

    Returns: iv_annual, iv_percentile (0-100 rank), hv_30, hv_252,
             atr_14, daily_expected_move, weekly_expected_move.
    """
    key = f"vol_{ticker.upper()}"
    return await _acached(key, 600, get_volatility_profile(ticker))


@app.get("/api/analysis/setup/{ticker}")
async def analysis_trade_setup(
    ticker: str,
    side:   str   = Query("long",  description="long or short"),
    entry:  float = Query(0.0,     description="Entry price (0 = current market price)"),
):
    """
    Complete IV-based trade setup for a ticker.

    Stop distance  = max(ATR×1.5, daily_expected_move×0.75)
    R:R ratio      = f(IV percentile):
      IVP  0-25  → 3.0:1   (low vol — hold for a large move)
      IVP 25-50  → 2.5:1
      IVP 50-75  → 2.0:1
      IVP 75-90  → 1.75:1
      IVP 90+    → 1.5:1   (high vol — tighter, mean-reversion risk)

    Confluence score (from /api/analysis/technical) optionally adjusts R:R by ±0.5.
    The stop and target are expressed in absolute price, %, AND $ per share.
    """
    if side.lower() not in ("long", "short"):
        raise HTTPException(status_code=400, detail="side must be 'long' or 'short'")
    return await get_trade_setup(ticker, side, entry)


@app.get("/api/analysis/full/{ticker}")
async def analysis_full(
    ticker: str,
    side:   str   = Query("long",  description="long or short"),
):
    """
    Combined endpoint: MTF analysis + volatility profile + trade setup in one call.
    Runs all three in parallel. Use this to power the full analysis view.
    """
    if side.lower() not in ("long", "short"):
        raise HTTPException(status_code=400, detail="side must be 'long' or 'short'")

    mtf_key = f"mtf_{ticker.upper()}"
    vol_key = f"vol_{ticker.upper()}"

    mtf_task = _acached(mtf_key, 300, get_mtf_analysis(ticker))
    vol_task = _acached(vol_key, 600, get_volatility_profile(ticker))

    mtf, vol = await asyncio.gather(mtf_task, vol_task)

    # Build trade setup using confluence score from MTF
    confluence = mtf.get("confluence_score", 0) if isinstance(mtf, dict) else 0
    price      = mtf.get("price", 0.0)          if isinstance(mtf, dict) else 0.0

    from analysis.volatility import calc_trade_setup
    setup = calc_trade_setup(ticker, price, side, vol, confluence) if price and vol else {}

    return {
        "ticker":    ticker.upper(),
        "side":      side.lower(),
        "mtf":       mtf,
        "vol":       vol,
        "setup":     setup,
        "timestamp": int(time.time()),
    }


# ── Nasdaq Data Link ──────────────────────────────────────────────────────────

@app.get("/api/nasdaq/status")
async def nasdaq_adapter_status():
    """Check whether Nasdaq Data Link API key is configured."""
    return nasdaq_status()


@app.get("/api/nasdaq/short-interest/{ticker}")
async def nasdaq_short_interest(ticker: str):
    """
    Official FINRA bi-weekly short interest for a ticker.
    Requires NASDAQ_DATA_LINK_API_KEY in .env.
    """
    data = await get_short_interest(ticker.upper())
    if not data:
        return {"ticker": ticker.upper(), "available": False,
                "note": "Set NASDAQ_DATA_LINK_API_KEY to enable FINRA data"}
    return {"ticker": ticker.upper(), "available": True, **data}


@app.get("/api/nasdaq/macro")
async def nasdaq_macro():
    """
    Latest macro indicators via Nasdaq Data Link / FRED.
    Requires NASDAQ_DATA_LINK_API_KEY in .env.
    """
    snapshot = await nasdaq_macro_snapshot()
    available = any(v is not None for v in snapshot.values())
    return {
        "available": available,
        "indicators": snapshot,
        "note": None if available else "Set NASDAQ_DATA_LINK_API_KEY to enable FRED macro data",
        "timestamp": int(time.time()),
    }


@app.get("/api/nasdaq/history/{ticker}")
async def nasdaq_history(
    ticker: str,
    start: str = Query("2024-01-01", description="YYYY-MM-DD"),
    source: str = Query("WIKI", description="WIKI or EOD"),
):
    """
    Historical OHLCV from Nasdaq Data Link.
    WIKI is free; EOD requires subscription.
    """
    data = await nasdaq_ohlcv(ticker.upper(), start_date=start, source=source)
    if not data:
        return {"ticker": ticker.upper(), "available": False,
                "note": "Set NASDAQ_DATA_LINK_API_KEY or try source=WIKI"}
    return {"ticker": ticker.upper(), "source": source, "bars": data, "count": len(data)}


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


# ── Equity AI Routes ──────────────────────────────────────────────────────────

@app.get("/api/equity/quick/{ticker}")
async def equity_quick_analysis(ticker: str):
    """
    Fast equity analysis using yfinance — no API keys required.

    Returns analyst consensus (buy/sell/hold), price targets from broker
    research, EPS estimates, institutional ownership, and recent insider
    activity.  Runs in ~3-5 seconds vs 30-60s for the LLM workflow.

    Use this when FINANCIAL_DATASETS_API_KEY / LLM keys are not configured,
    or whenever you want a quick data-driven view without waiting for AI.
    """
    from data.yfinance_adapter import get_analyst_info, get_quote, get_news
    from analysis.news_correlator import correlate_symbol_news

    sym = ticker.upper().strip()
    loop = asyncio.get_event_loop()

    # Run all fetches concurrently
    analyst_t = loop.run_in_executor(_executor, get_analyst_info, sym)
    news_t    = loop.run_in_executor(_executor, correlate_symbol_news, sym)

    analyst_data, news_data = await asyncio.gather(
        analyst_t, news_t, return_exceptions=True
    )

    if isinstance(analyst_data, Exception):
        analyst_data = {"symbol": sym}
    if isinstance(news_data, Exception):
        news_data = {}

    info = analyst_data.get("info_summary", {})
    pt   = analyst_data.get("price_targets", {})
    rec  = analyst_data.get("recommendations", [])
    ee   = analyst_data.get("earnings_estimate", {})

    # Build a quick verdict from analyst consensus
    rec_key = info.get("recommendation", "").lower()
    verdict_map = {
        "strong_buy": "STRONG BUY",  "buy": "BUY",
        "hold": "HOLD",              "underperform": "UNDERPERFORM",
        "sell": "SELL",
    }
    verdict = verdict_map.get(rec_key, "NO RATING")

    # Count recent upgrades vs downgrades
    upgrades   = sum(1 for r in rec if str(r.get("To Grade", "")).lower() in ("buy", "strong buy", "outperform", "overweight"))
    downgrades = sum(1 for r in rec if str(r.get("To Grade", "")).lower() in ("sell", "underperform", "underweight"))

    return {
        "ticker":          sym,
        "verdict":         verdict,
        "recommendation":  rec_key,
        "upgrades_recent": upgrades,
        "downgrades_recent": downgrades,
        "price_targets":   pt,
        "earnings_estimate": ee,
        "info":            info,
        "recent_actions":  rec[:8],
        "institutional":   analyst_data.get("institutional", [])[:5],
        "insiders":        analyst_data.get("insiders", [])[:6],
        "catalyst":        {
            "primary_driver": news_data.get("primary_driver"),
            "driver_label":   news_data.get("driver_label"),
            "summary":        news_data.get("summary"),
            "change_pct":     news_data.get("change_pct"),
        },
        "data_source":     "yfinance (no API key required)",
        "timestamp":       int(time.time()),
    }


@app.get("/api/equity/status")
async def equity_ai_status():
    """
    Check whether the equity AI analyst engine is available.

    Returns availability flags and any missing-configuration warnings.
    The engine requires:
      • FINANCIAL_DATASETS_API_KEY — fundamental data for analyst agents
      • At least one LLM key (OPENAI_API_KEY / ANTHROPIC_API_KEY / GROQ_API_KEY)
      • langchain + langgraph installed (pip install langchain langgraph langchain-openai)
    """
    return _equity_status()


@app.get("/api/equity/agents")
async def equity_agents():
    """
    List all available analyst agents with display names, descriptions,
    investing styles, and ordering index.
    """
    agents = _list_agents()
    return {
        "agents": agents,
        "count":  len(agents),
    }


@app.post("/api/equity/analyze")
async def equity_analyze(
    tickers:    str  = Query(...,         description="Comma-separated tickers e.g. AAPL,NVDA,TSLA"),
    analysts:   str  = Query("",          description="Comma-separated agent keys (empty = all agents)"),
    model:      str  = Query("",          description="LLM model name (default from config)"),
    provider:   str  = Query("",          description="LLM provider: OpenAI, Anthropic, Groq, DeepSeek"),
    start_date: str  = Query("",          description="YYYY-MM-DD (default 3 months back)"),
    end_date:   str  = Query("",          description="YYYY-MM-DD (default today)"),
    reasoning:  bool = Query(False,       description="Include full agent reasoning text"),
):
    """
    Run the full ai-hedge-fund analyst workflow on the provided tickers.

    Each selected analyst agent independently evaluates every ticker and emits
    a signal (bullish / bearish / neutral) with a confidence score and reasoning.
    The portfolio manager then synthesises a final action recommendation.

    Returns:
      decisions       — portfolio manager's final action per ticker
      analyst_signals — every agent's raw signal per ticker
      summary         — per-ticker bull/bear/neutral vote counts
    """
    status = _equity_status()
    if not status["available"]:
        missing = "; ".join(status["warnings"])
        raise HTTPException(
            status_code=503,
            detail=f"Equity AI unavailable: {missing}",
        )

    ticker_list   = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    analyst_list  = [a.strip() for a in analysts.split(",") if a.strip()] if analysts else None

    if not ticker_list:
        raise HTTPException(status_code=400, detail="At least one ticker required")
    if len(ticker_list) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 tickers per request")

    try:
        result = await _run_equity_analysis(
            tickers=ticker_list,
            analysts=analyst_list,
            model_name=model or None,
            model_provider=provider or None,
            start_date=start_date or None,
            end_date=end_date or None,
            show_reasoning=reasoning,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("Equity analysis failed")
        raise HTTPException(status_code=500, detail=f"Analysis error: {e}")

    return result


@app.get("/api/equity/analyze/{ticker}")
async def equity_analyze_single(
    ticker:   str,
    analysts: str  = Query("",    description="Comma-separated agent keys (empty = all)"),
    model:    str  = Query("",    description="LLM model name"),
    provider: str  = Query("",    description="LLM provider"),
):
    """
    Convenience GET endpoint for a single ticker — same output as POST /api/equity/analyze.
    Useful for quick lookups without constructing a request body.
    """
    status = _equity_status()
    if not status["available"]:
        missing = "; ".join(status["warnings"])
        raise HTTPException(status_code=503, detail=f"Equity AI unavailable: {missing}")

    analyst_list = [a.strip() for a in analysts.split(",") if a.strip()] if analysts else None

    try:
        result = await _run_equity_analysis(
            tickers=[ticker.upper()],
            analysts=analyst_list,
            model_name=model or None,
            model_provider=provider or None,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("Equity single-ticker analysis failed")
        raise HTTPException(status_code=500, detail=f"Analysis error: {e}")

    return result


# ── Interactive Brokers Routes ────────────────────────────────────────────────
# Requires the IBKR Client Portal Gateway running locally at localhost:5000.
# Authenticate once in your browser at https://localhost:5000 then these
# routes proxy your requests through to IBKR.

@app.get("/api/ibkr/status")
async def ibkr_status():
    """
    Check if the Client Portal Gateway is reachable and the session is authenticated.
    Returns connection status, auth state, and list of accounts.
    """
    from trading.ibkr_adapter import get_auth_status, get_accounts
    loop = asyncio.get_event_loop()
    status = await loop.run_in_executor(_executor, get_auth_status)
    accounts: List[str] = []
    if status.get("authenticated"):
        accounts = await loop.run_in_executor(_executor, get_accounts)
    return {**status, "accounts": accounts}


@app.get("/api/ibkr/portfolio")
async def ibkr_portfolio(account_id: str = Query(..., description="IBKR account ID e.g. U1234567")):
    """
    Full portfolio snapshot: net liquidation, cash, buying power, open positions, P&L.
    Requires authenticated gateway session.
    """
    from trading.ibkr_adapter import get_portfolio_snapshot
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(_executor, get_portfolio_snapshot, account_id)
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"IBKR error: {e}")


@app.get("/api/ibkr/orders")
async def ibkr_orders():
    """All open/pending orders across accounts."""
    from trading.ibkr_adapter import get_open_orders
    loop = asyncio.get_event_loop()
    try:
        orders = await loop.run_in_executor(_executor, get_open_orders)
        return {"orders": orders, "count": len(orders)}
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/ibkr/quote/{symbol}")
async def ibkr_quote(symbol: str):
    """Live quote for a stock via IBKR (last, bid, ask, 52w range)."""
    from trading.ibkr_adapter import get_stock_quote
    loop = asyncio.get_event_loop()
    try:
        quote = await loop.run_in_executor(_executor, get_stock_quote, symbol.upper())
        if not quote:
            raise HTTPException(status_code=404, detail=f"No contract found for {symbol}")
        return quote
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/api/ibkr/order")
async def ibkr_place_order(
    account_id:  str   = Query(..., description="IBKR account ID"),
    symbol:      str   = Query(..., description="Ticker e.g. AAPL"),
    action:      str   = Query(..., description="BUY or SELL"),
    quantity:    float = Query(..., description="Number of shares"),
    order_type:  str   = Query("MKT", description="MKT | LMT | STP | STP LMT"),
    limit_price: Optional[float] = Query(None, description="Limit price (LMT orders)"),
    stop_price:  Optional[float] = Query(None, description="Stop price (STP orders)"),
    tif:         str   = Query("DAY", description="DAY | GTC | IOC"),
    outside_rth: bool  = Query(False, description="Allow pre/after-market execution"),
):
    """
    Place a stock order via IBKR Client Portal API.
    Automatically resolves symbol → contract ID, then submits the order.
    Handles IBKR's confirmation reply flow automatically.

    Paper trading: authenticate the gateway with your paper account.
    Live trading:  authenticate with your live account.
    """
    from trading.ibkr_adapter import search_contract, place_order
    loop = asyncio.get_event_loop()

    if action.upper() not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="action must be BUY or SELL")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")

    try:
        contract = await loop.run_in_executor(_executor, search_contract, symbol.upper())
        if not contract or not contract.get("conid"):
            raise HTTPException(status_code=404, detail=f"No IBKR contract found for {symbol}")

        result = await loop.run_in_executor(
            _executor, place_order,
            account_id, contract["conid"], action.upper(), quantity,
            order_type, limit_price, stop_price, tif, outside_rth,
        )
        return {"contract": contract, "order_result": result}
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("IBKR order failed")
        raise HTTPException(status_code=500, detail=f"Order error: {e}")


@app.delete("/api/ibkr/order/{order_id}")
async def ibkr_cancel_order(
    order_id:   str = ...,
    account_id: str = Query(..., description="IBKR account ID"),
):
    """Cancel an open IBKR order."""
    from trading.ibkr_adapter import cancel_order
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(_executor, cancel_order, account_id, order_id)
        return result
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/api/ibkr/tickle")
async def ibkr_tickle():
    """Keep the gateway session alive. Call every 60 seconds from the frontend."""
    from trading.ibkr_adapter import tickle
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_executor, tickle)
    return {"ok": ok}


# ── IBKR Strategy + Trade Journal Routes ─────────────────────────────────────

@app.get("/api/ibkr/strategy/signals")
async def ibkr_strategy_signals(top_n: int = Query(20, ge=1, le=50)):
    """
    Run the full signal pipeline: momentum scan → social sentiment → entry evaluation.
    Returns ranked list of candidates with BUY / HOLD / AVOID verdicts and all
    the conditions that led to each decision.
    """
    from trading.strategy_engine import scan_for_entries
    result = await _acached("strategy_signals", 120, scan_for_entries(top_n=top_n))
    return {
        "signals":   [s.to_dict() for s in result],
        "buy_count": sum(1 for s in result if s.action == "BUY"),
        "timestamp": int(time.time()),
    }


@app.get("/api/ibkr/strategy/evaluate/{symbol}")
async def ibkr_strategy_evaluate(symbol: str):
    """
    Deep signal evaluation for a single symbol:
    momentum + social sentiment + technical + news catalyst combined.
    """
    from trading.strategy_engine import compute_composite_signal
    from data.yfinance_adapter import get_momentum_data
    from data.adanos_adapter import get_sentiment_snapshot
    from analysis.news_correlator import correlate_symbol_news

    sym = symbol.upper().strip()
    loop = asyncio.get_event_loop()

    mom_t  = loop.run_in_executor(_executor, get_momentum_data, sym)
    sent_t = loop.run_in_executor(_executor, get_sentiment_snapshot, sym, 3)
    news_t = loop.run_in_executor(_executor, correlate_symbol_news, sym)

    mom, sent, news = await asyncio.gather(mom_t, sent_t, news_t, return_exceptions=True)
    if isinstance(mom,  Exception): mom  = None
    if isinstance(sent, Exception): sent = None
    if isinstance(news, Exception): news = {}

    # Build momentum_data dict in expected format
    mom_data = None
    if mom:
        from analysis.momentum_scanner import _score, _detect_signals
        mom_data = {**mom, "score": _score(mom), "signals": _detect_signals(mom)}

    sig = compute_composite_signal(sym, mom_data, sent, news_data=news)
    return sig.to_dict()


@app.post("/api/ibkr/strategy/size")
async def ibkr_strategy_size(
    symbol:        str   = Query(...),
    account_id:    str   = Query(...),
    entry_price:   float = Query(...),
):
    """Calculate recommended position size for a symbol given the account balance."""
    from trading.strategy_engine import calc_position_size
    from trading.ibkr_adapter import get_account_summary
    loop = asyncio.get_event_loop()
    summary = await loop.run_in_executor(_executor, get_account_summary, account_id)
    account_value = summary.get("NetLiquidation") or summary.get("TotalCashValue") or 0
    sizing = calc_position_size(account_value, entry_price)
    return {
        "symbol":          symbol.upper(),
        "account_value":   account_value,
        "entry_price":     entry_price,
        **sizing,
    }


# ── VORTEX-1 Routes ───────────────────────────────────────────────────────────
# Citadel-style quant strategy: Volatility-Anchored Momentum + Sentiment
# Exhaustion Reversal. 5-filter confluence entry, dual-TP exits, regime filter.

@app.get("/api/ibkr/vortex/regime")
async def vortex_regime():
    """
    Current market regime from SPY 50D/200D SMA + VIX.
    Returns BULL / BEAR / CHOP with the underlying indicator values.
    """
    from trading.vortex_strategy import get_market_regime, MarketRegime, _sma
    import yfinance as yf

    loop = asyncio.get_event_loop()

    def _fetch():
        vix_val  = float(yf.Ticker("^VIX").fast_info.last_price)
        spy      = yf.Ticker("SPY")
        spy_d    = spy.history(period="300d", interval="1d")
        spy_c    = spy_d["Close"]
        sma50    = float(_sma(spy_c, 50).iloc[-1])
        sma200   = float(_sma(spy_c, 200).iloc[-1])
        price    = float(spy_c.iloc[-1])
        regime   = get_market_regime(vix_val, spy_c)
        return {
            "regime":    regime.value,
            "vix":       round(vix_val, 2),
            "spy_price": round(price, 2),
            "spy_sma50": round(sma50, 2),
            "spy_sma200":round(sma200, 2),
            "above_50d": price > sma50,
            "above_200d":price > sma200,
            "regime_rules": {
                "BULL": "SPY > 50D & 200D SMA, VIX < 20 → long only, TP=3R",
                "BEAR": "SPY < 50D & 200D SMA, VIX > 25 → short/cash, size×0.5",
                "CHOP": "between → max 1 trade/day, highest conviction only",
            },
            "timestamp": int(time.time()),
        }

    try:
        result = await loop.run_in_executor(_executor, _fetch)
        return result
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"regime fetch failed: {exc}")


@app.get("/api/ibkr/vortex/scan")
async def vortex_scan(
    account_value: float = Query(1000.0, ge=100, description="Account value for position sizing"),
):
    """
    Scan the full VORTEX-1 universe (SPY, QQQ, IWM, NVDA, AAPL, TSLA, AMZN,
    META, XLK, XLE, SOXX) and evaluate all 5 entry filters per symbol.
    Returns ranked list — BUY_LONG signals first, then by filter score.
    Cached 5 minutes.
    """
    from trading.vortex_strategy import scan_vortex_universe
    loop   = asyncio.get_event_loop()
    result = await _acached(
        f"vortex_scan_{int(account_value)}",
        300,
        loop.run_in_executor(_executor, scan_vortex_universe, account_value),
    )
    return result


@app.get("/api/ibkr/vortex/evaluate/{symbol}")
async def vortex_evaluate(
    symbol:        str,
    account_value: float = Query(1000.0, ge=100),
):
    """
    Evaluate all 5 VORTEX-1 entry filters for a single symbol.
    Returns filter-by-filter pass/fail reasoning plus entry/stop/TP prices
    and position sizing if all 5 filters pass.
    """
    from trading.vortex_strategy import (
        evaluate_entry, get_market_regime, MarketRegime, _hv_rank,
    )
    import yfinance as yf

    sym  = symbol.upper().strip()
    loop = asyncio.get_event_loop()

    def _fetch():
        t      = yf.Ticker(sym)
        hourly = t.history(period="60d",  interval="1h")
        daily  = t.history(period="300d", interval="1d")
        vix    = float(yf.Ticker("^VIX").fast_info.last_price)
        spy_d  = yf.Ticker("SPY").history(period="300d", interval="1d")
        regime = get_market_regime(vix, spy_d["Close"])
        ivr    = _hv_rank(daily["Close"])
        sig    = evaluate_entry(sym, hourly, daily, vix, ivr, regime, account_value)
        return sig.to_dict()

    try:
        return await loop.run_in_executor(_executor, _fetch)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/ibkr/vortex/backtest")
async def vortex_backtest(
    symbol:          str   = Query(..., description="Ticker to backtest (e.g. SPY)"),
    start_date:      Optional[str] = Query(None, description="YYYY-MM-DD start"),
    end_date:        Optional[str] = Query(None, description="YYYY-MM-DD end"),
    initial_capital: float = Query(1000.0, ge=100, description="Starting capital"),
    max_days:        int   = Query(365, ge=30, le=730, description="Max history days"),
):
    """
    Walk-forward bar-by-bar backtest of VORTEX-1 on historical 1H data.

    Returns per-trade log, equity curve, win rate, Sharpe, max drawdown,
    profit factor, expected R per trade, and regime-breakdown stats.
    Note: yfinance caps 1H data at ~730 days. Use max_days <= 365 for reliability.
    """
    from trading.vortex_backtester import VortexBacktester
    loop = asyncio.get_event_loop()

    def _run():
        bt = VortexBacktester(initial_capital=initial_capital)
        return bt.run(
            symbol     = symbol.upper().strip(),
            start_date = start_date,
            end_date   = end_date,
            max_days   = max_days,
        ).to_dict()

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_executor, _run),
            timeout=120,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="backtest timed out (120s). Try a shorter date range.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/ibkr/vortex/size")
async def vortex_size(
    symbol:        str   = Query(...),
    entry_price:   float = Query(..., gt=0),
    stop_price:    float = Query(..., gt=0),
    account_value: float = Query(1000.0, ge=100),
):
    """
    VORTEX-1 Kelly-adjusted position sizing.
    Accounts for current regime (BULL/BEAR/CHOP) — bear regime halves size.
    Returns shares, dollar risk, dollar size, TP1/TP2 levels, and risk %.
    """
    from trading.vortex_strategy import (
        calc_vortex_size, get_market_regime, MarketRegime,
    )
    import yfinance as yf

    sym  = symbol.upper().strip()
    loop = asyncio.get_event_loop()

    def _fetch():
        vix   = float(yf.Ticker("^VIX").fast_info.last_price)
        spy_d = yf.Ticker("SPY").history(period="300d", interval="1d")
        regime= get_market_regime(vix, spy_d["Close"])
        sizing= calc_vortex_size(account_value, entry_price, stop_price, regime)
        return {"symbol": sym, "account_value": account_value, **sizing}

    try:
        return await loop.run_in_executor(_executor, _fetch)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ── Trade Journal Routes ──────────────────────────────────────────────────────

@app.get("/api/journal/trades")
async def journal_trades(
    limit:       int            = Query(100, ge=1, le=500),
    symbol:      Optional[str]  = Query(None),
    account_id:  str            = Query(""),
    include_open: bool          = Query(False, description="Include open (not yet closed) trades"),
):
    """Trade history: all closed trades. Set include_open=true to see open positions too."""
    from trading.trade_journal import get_trade_history
    loop = asyncio.get_event_loop()
    trades = await loop.run_in_executor(
        _executor, get_trade_history, limit, symbol, account_id, not include_open
    )
    return {"trades": trades, "count": len(trades)}


@app.get("/api/journal/open")
async def journal_open(account_id: str = Query("")):
    """All currently open (un-exited) journal positions."""
    from trading.trade_journal import get_open_trades
    loop = asyncio.get_event_loop()
    trades = await loop.run_in_executor(_executor, get_open_trades, account_id)
    return {"trades": trades, "count": len(trades)}


@app.get("/api/journal/stats")
async def journal_stats(account_id: str = Query("")):
    """
    Performance statistics and signal accuracy (learning loop).
    Shows win rate, avg P&L, best/worst trades, and which signals have
    the best track record — so the strategy can be tuned over time.
    """
    from trading.trade_journal import get_performance_stats
    loop = asyncio.get_event_loop()
    stats = await loop.run_in_executor(_executor, get_performance_stats, account_id)
    return stats


@app.post("/api/journal/entry")
async def journal_record_entry(
    symbol:       str   = Query(...),
    entry_price:  float = Query(...),
    shares:       float = Query(...),
    account_id:   str   = Query(""),
    ibkr_order_id: str  = Query(""),
):
    """
    Manually record a trade entry in the journal.
    The strategy bot calls this automatically; you can also call it manually
    to log trades placed directly in TWS.
    """
    from trading.trade_journal import record_entry
    from trading.strategy_engine import compute_composite_signal
    from data.yfinance_adapter import get_momentum_data

    sym = symbol.upper().strip()
    loop = asyncio.get_event_loop()
    mom = await loop.run_in_executor(_executor, get_momentum_data, sym)

    mom_data = None
    if mom:
        from analysis.momentum_scanner import _score, _detect_signals
        mom_data = {**mom, "score": _score(mom), "signals": _detect_signals(mom)}

    sig = compute_composite_signal(sym, mom_data)
    trade_id = await loop.run_in_executor(
        _executor, record_entry, sym, entry_price, shares, sig, account_id, ibkr_order_id
    )
    return {"trade_id": trade_id, "symbol": sym, "entry_price": entry_price, "shares": shares}


@app.post("/api/journal/exit/{trade_id}")
async def journal_record_exit(
    trade_id:    int,
    exit_price:  float = Query(...),
    exit_reason: str   = Query("MANUAL", description="STOP_LOSS|TAKE_PROFIT|TRAILING|TIME|SIGNAL_REVERSAL|MANUAL"),
    fees:        float = Query(0.0),
    notes:       str   = Query(""),
):
    """Record the exit of a trade and compute final P&L + update signal accuracy stats."""
    from trading.trade_journal import record_exit
    loop = asyncio.get_event_loop()
    trade = await loop.run_in_executor(
        _executor, record_exit, trade_id, exit_price, exit_reason, fees, "", notes
    )
    return trade


@app.get("/api/journal/dashboard")
async def journal_dashboard(account_id: str = Query("")):
    """
    Combined IBKR + journal dashboard payload.
    Single request to populate the full trading dashboard:
      - Gateway status + accounts
      - Portfolio summary (if gateway connected)
      - Open positions (IBKR + journal open trades)
      - Open orders
      - Recent trade history (last 20)
      - Performance stats
      - Current strategy signals (top 10 BUY candidates)
    """
    from trading.ibkr_adapter import get_auth_status, get_accounts, get_portfolio_snapshot, get_open_orders
    from trading.trade_journal import get_open_trades, get_trade_history, get_performance_stats
    from trading.strategy_engine import scan_for_entries

    loop = asyncio.get_event_loop()

    # Run everything concurrently
    gw_status_t  = loop.run_in_executor(_executor, get_auth_status)
    jrnl_open_t  = loop.run_in_executor(_executor, get_open_trades, account_id)
    history_t    = loop.run_in_executor(_executor, get_trade_history, 20, None, account_id, True)
    stats_t      = loop.run_in_executor(_executor, get_performance_stats, account_id)
    signals_task = scan_for_entries(top_n=10)

    gw_status, jrnl_open, history, stats = await asyncio.gather(
        gw_status_t, jrnl_open_t, history_t, stats_t, return_exceptions=True
    )
    signals = await signals_task

    if isinstance(gw_status, Exception): gw_status = {"authenticated": False}
    if isinstance(jrnl_open, Exception): jrnl_open = []
    if isinstance(history,   Exception): history   = []
    if isinstance(stats,     Exception): stats     = {}

    # Fetch IBKR live data only if gateway is connected
    portfolio = {}
    ibkr_orders: List[Dict] = []
    if isinstance(gw_status, dict) and gw_status.get("authenticated") and account_id:
        try:
            portfolio   = await loop.run_in_executor(_executor, get_portfolio_snapshot, account_id)
            ibkr_orders = await loop.run_in_executor(_executor, get_open_orders)
        except Exception:
            pass

    return {
        "gateway":          gw_status,
        "portfolio":        portfolio,
        "ibkr_orders":      ibkr_orders,
        "journal_open":     jrnl_open,
        "trade_history":    history,
        "performance":      stats,
        "top_signals":      [s.to_dict() for s in signals if s.action == "BUY"][:5],
        "timestamp":        int(time.time()),
    }


# ── Finance-Skills Equity Research Routes ────────────────────────────────────
# Powered by Funda AI (fundamentals, earnings, filings) and
# Adanos Finance (social sentiment across Reddit, X, news, Polymarket).
# All routes degrade gracefully — returns partial data when API keys are absent.

@app.get("/api/research/snapshot/{ticker}")
async def research_snapshot(ticker: str):
    """
    One-shot equity research snapshot: fundamentals + analyst targets +
    earnings history + social sentiment.
    Requires FUNDA_API_KEY and/or ADANOS_API_KEY for full data.
    Falls back to yfinance data when keys are absent.
    """
    from data.funda_adapter import get_equity_snapshot
    from data.adanos_adapter import get_sentiment_snapshot
    from data.yfinance_adapter import get_quote

    sym = ticker.upper().strip()

    loop = asyncio.get_event_loop()
    funda_task    = loop.run_in_executor(_executor, get_equity_snapshot, sym)
    adanos_task   = loop.run_in_executor(_executor, get_sentiment_snapshot, sym)
    yf_task       = loop.run_in_executor(_executor, get_quote, sym)

    funda_data, adanos_data, yf_quote = await asyncio.gather(
        funda_task, adanos_task, yf_task, return_exceptions=True
    )

    if isinstance(funda_data, Exception):
        funda_data = {}
    if isinstance(adanos_data, Exception):
        adanos_data = {}
    if isinstance(yf_quote, Exception):
        yf_quote = {}

    return {
        "ticker":         sym,
        "quote":          yf_quote or {},
        "fundamentals":   funda_data,
        "sentiment":      adanos_data,
        "timestamp":      int(__import__("time").time()),
    }


@app.get("/api/research/sentiment/{ticker}")
async def research_sentiment(
    ticker: str,
    days:   int = Query(7, ge=1, le=30, description="Lookback window in days"),
):
    """
    Cross-source social sentiment for a ticker.
    Sources: Reddit, X.com, News, Polymarket.
    Requires ADANOS_API_KEY.
    """
    from data.adanos_adapter import get_sentiment_snapshot
    sym = ticker.upper().strip()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, get_sentiment_snapshot, sym, days)
    return result


@app.get("/api/research/earnings-calendar")
async def research_earnings_calendar(
    date_after:  Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_before: Optional[str] = Query(None, description="YYYY-MM-DD"),
    ticker:      Optional[str] = Query(None, description="Filter by ticker"),
    limit:       int           = Query(50, ge=1, le=200),
):
    """
    Upcoming earnings announcements.
    Requires FUNDA_API_KEY for full data; returns empty list otherwise.
    """
    from data.funda_adapter import get_earnings_calendar
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(
        _executor, get_earnings_calendar, date_after, date_before, ticker, limit
    )
    return {"earnings": data, "count": len(data)}


@app.get("/api/research/economic-calendar")
async def research_economic_calendar(
    date_after:  Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_before: Optional[str] = Query(None, description="YYYY-MM-DD"),
):
    """
    Upcoming macro events: Fed decisions, CPI, GDP, NFP.
    Requires FUNDA_API_KEY.
    """
    from data.funda_adapter import get_economic_calendar
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(_executor, get_economic_calendar, date_after, date_before)
    return {"events": data, "count": len(data)}


@app.get("/api/research/fundamentals/{ticker}")
async def research_fundamentals(
    ticker: str,
    period: str = Query("annual", description="annual or quarter"),
):
    """
    Key metrics TTM + income statement + analyst estimates for a ticker.
    Requires FUNDA_API_KEY.
    """
    from data.funda_adapter import get_key_metrics_ttm, get_income_statement, get_analyst_estimates, get_price_targets
    sym = ticker.upper().strip()
    loop = asyncio.get_event_loop()

    metrics_t  = loop.run_in_executor(_executor, get_key_metrics_ttm, sym)
    income_t   = loop.run_in_executor(_executor, get_income_statement, sym, period, 4)
    estimates_t = loop.run_in_executor(_executor, get_analyst_estimates, sym)
    targets_t  = loop.run_in_executor(_executor, get_price_targets, sym)

    metrics, income, estimates, targets = await asyncio.gather(
        metrics_t, income_t, estimates_t, targets_t, return_exceptions=True
    )

    return {
        "ticker":            sym,
        "key_metrics_ttm":   metrics if not isinstance(metrics, Exception) else None,
        "income_statement":  income  if not isinstance(income, Exception) else [],
        "analyst_estimates": estimates if not isinstance(estimates, Exception) else None,
        "price_targets":     targets if not isinstance(targets, Exception) else None,
    }


@app.get("/api/research/catalyst-insight/{ticker}")
async def research_catalyst_insight(ticker: str):
    """
    Why is this stock moving? Combines:
      • Recent news sentiment (yfinance + Finnhub)
      • Social buzz from Adanos (Reddit, X, news, Polymarket)
      • Upcoming earnings date (Funda AI)
      • Recent SEC 8-K filings (Funda AI)
    Returns a structured explanation of the primary catalyst.
    """
    from analysis.news_correlator import correlate_symbol_news
    from data.adanos_adapter import get_sentiment_snapshot
    from data.funda_adapter import get_earnings_calendar, get_sec_filings
    import time as _time

    sym = ticker.upper().strip()
    loop = asyncio.get_event_loop()

    # Run all concurrently
    news_t    = loop.run_in_executor(_executor, correlate_symbol_news, sym)
    sent_t    = loop.run_in_executor(_executor, get_sentiment_snapshot, sym, 7)
    earn_t    = loop.run_in_executor(_executor, get_earnings_calendar, None, None, sym, 3)
    filings_t = loop.run_in_executor(_executor, get_sec_filings, sym, "8-K", 5)

    news_data, sentiment, earnings, filings = await asyncio.gather(
        news_t, sent_t, earn_t, filings_t, return_exceptions=True
    )

    if isinstance(news_data, Exception):
        news_data = {}
    if isinstance(sentiment, Exception):
        sentiment = {}
    if isinstance(earnings, Exception):
        earnings = []
    if isinstance(filings, Exception):
        filings = []

    # Build catalyst explanation
    explanation_parts = []
    change_pct = news_data.get("change_pct", 0)

    # 1. Primary market driver from news
    driver_label = news_data.get("driver_label", "")
    if driver_label:
        direction = "up" if change_pct > 0 else "down"
        explanation_parts.append(
            f"{sym} is {direction} {abs(change_pct):.2f}% — primary driver: {driver_label}."
        )

    # 2. Social sentiment signal
    comp = (sentiment or {}).get("composite", {})
    if comp.get("sentiment") and comp["sentiment"] != "neutral":
        bull_pct = comp.get("bullish_pct") or 0
        explanation_parts.append(
            f"Social sentiment is {comp['sentiment']} ({bull_pct*100:.0f}% bullish across platforms)."
        )
        if comp.get("sources_agree"):
            explanation_parts.append("All tracked sources are aligned.")

    # 3. Upcoming earnings catalyst
    if earnings:
        next_earn = earnings[0]
        explanation_parts.append(
            f"Next earnings: {next_earn.get('date', 'TBD')} "
            f"({'before open' if next_earn.get('time') == 'bmo' else 'after close'})."
        )

    # 4. Recent 8-K filings
    if filings:
        explanation_parts.append(
            f"{len(filings)} recent 8-K filing(s) — latest: {filings[0].get('date', 'unknown date')}."
        )

    return {
        "ticker":           sym,
        "change_pct":       round(change_pct, 3),
        "explanation":      " ".join(explanation_parts) or f"No strong catalyst identified for {sym} today.",
        "primary_driver":   news_data.get("primary_driver"),
        "driver_label":     news_data.get("driver_label"),
        "social_sentiment": comp,
        "upcoming_earnings": earnings[:1] if earnings else [],
        "recent_filings":   filings[:3],
        "news":             news_data.get("news", [])[:5],
        "timestamp":        int(_time.time()),
    }


# ── Settings Routes ───────────────────────────────────────────────────────────

# All keys managed through the Settings UI, grouped by subsystem.
# Sensitive keys are write-only — we return a boolean "is_set", never the value.

_ENV_FILE = Path(__file__).parent / ".env"

# Key definitions: (env_var, display_label, sensitive, placeholder)
_KEY_DEFS = {
    # ── Market Data ──────────────────────────────────────────────────────────
    "FINNHUB_API_KEY": {
        "group":       "Market Data",
        "label":       "Finnhub API Key",
        "sensitive":   True,
        "placeholder": "Enables real-time stock news & analyst recommendations",
        "docs_url":    "https://finnhub.io",
    },
    "FRED_API_KEY": {
        "group":       "Market Data",
        "label":       "FRED API Key",
        "sensitive":   True,
        "placeholder": "Federal Reserve macro indicators (yield curve, CPI, etc.)",
        "docs_url":    "https://fred.stlouisfed.org/docs/api/api_key.html",
    },
    "NASDAQ_DATA_LINK_API_KEY": {
        "group":       "Market Data",
        "label":       "Nasdaq Data Link API Key",
        "sensitive":   True,
        "placeholder": "FINRA short interest, EOD history, macro data",
        "docs_url":    "https://data.nasdaq.com/account/profile",
    },
    # ── Polymarket Live Trading ──────────────────────────────────────────────
    "POLY_PRIVATE_KEY": {
        "group":       "Polymarket Live Trading",
        "label":       "Polygon Wallet Private Key",
        "sensitive":   True,
        "placeholder": "0x… — Polygon mainnet wallet that holds USDC",
        "docs_url":    "https://docs.polymarket.com",
    },
    "POLY_FUNDER": {
        "group":       "Polymarket Live Trading",
        "label":       "Polymarket Funder Address",
        "sensitive":   False,
        "placeholder": "0x… — proxy/funder address shown in Polymarket wallet",
        "docs_url":    "https://docs.polymarket.com",
    },
    "POLY_HOST": {
        "group":       "Polymarket Live Trading",
        "label":       "CLOB Host (optional)",
        "sensitive":   False,
        "placeholder": "https://clob.polymarket.com  (leave blank for default)",
        "docs_url":    "https://docs.polymarket.com",
    },
    # ── Equity AI ────────────────────────────────────────────────────────────
    "FINANCIAL_DATASETS_API_KEY": {
        "group":       "Equity AI",
        "label":       "Financial Datasets API Key",
        "sensitive":   True,
        "placeholder": "financialdatasets.ai — prices, metrics, insider trades",
        "docs_url":    "https://financialdatasets.ai",
    },
    "OPENAI_API_KEY": {
        "group":       "Equity AI",
        "label":       "OpenAI API Key",
        "sensitive":   True,
        "placeholder": "sk-… — GPT-4o-mini recommended for cost/speed",
        "docs_url":    "https://platform.openai.com/api-keys",
    },
    "ANTHROPIC_API_KEY": {
        "group":       "Equity AI",
        "label":       "Anthropic API Key",
        "sensitive":   True,
        "placeholder": "sk-ant-… — Claude Haiku for fast + cheap analysis",
        "docs_url":    "https://console.anthropic.com",
    },
    "GROQ_API_KEY": {
        "group":       "Equity AI",
        "label":       "Groq API Key",
        "sensitive":   True,
        "placeholder": "gsk_… — fastest & free tier available",
        "docs_url":    "https://console.groq.com",
    },
    "DEEPSEEK_API_KEY": {
        "group":       "Equity AI",
        "label":       "DeepSeek API Key",
        "sensitive":   True,
        "placeholder": "Very cheap per-token pricing",
        "docs_url":    "https://platform.deepseek.com",
    },
    "EQUITY_MODEL_NAME": {
        "group":       "Equity AI",
        "label":       "Model Name (optional)",
        "sensitive":   False,
        "placeholder": "gpt-4o-mini  (default)",
        "docs_url":    "",
    },
    "EQUITY_MODEL_PROVIDER": {
        "group":       "Equity AI",
        "label":       "Model Provider (optional)",
        "sensitive":   False,
        "placeholder": "OpenAI  (OpenAI / Anthropic / Groq / DeepSeek)",
        "docs_url":    "",
    },
    # ── Finance-Skills Data Providers ────────────────────────────────────────
    "FUNDA_API_KEY": {
        "group":       "Research Data Providers",
        "label":       "Funda AI API Key",
        "sensitive":   True,
        "placeholder": "Earnings calendar, fundamentals, options flow, SEC filings, supply chain",
        "docs_url":    "https://funda.ai",
    },
    "ADANOS_API_KEY": {
        "group":       "Research Data Providers",
        "label":       "Adanos Finance API Key",
        "sensitive":   True,
        "placeholder": "Reddit / X.com / news / Polymarket sentiment per ticker",
        "docs_url":    "https://api.adanos.org/docs",
    },
    # ── Interactive Brokers ───────────────────────────────────────────────────
    "IBKR_GATEWAY_URL": {
        "group":       "Interactive Brokers",
        "label":       "Gateway URL (optional)",
        "sensitive":   False,
        "placeholder": "https://localhost:5000  (default — change if gateway runs elsewhere)",
        "docs_url":    "https://www.interactivebrokers.com/en/trading/ib-api.php",
    },
}


def _read_env_file() -> dict[str, str]:
    """Read the .env file into a dict. Returns {} if file doesn't exist."""
    env: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _write_env_file(env: dict[str, str]) -> None:
    """Write a dict back to the .env file, preserving comments and order."""
    existing_lines: list[str] = []
    if _ENV_FILE.exists():
        existing_lines = _ENV_FILE.read_text().splitlines()

    # Build updated set of key=value lines, preserving comments
    written: set[str] = set()
    output: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            output.append(line)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in env:
                if env[key]:   # only write if value non-empty
                    output.append(f'{key}={env[key]}')
                else:
                    output.append(line)  # keep existing if new value blank
                written.add(key)
            else:
                output.append(line)
        else:
            output.append(line)

    # Append any new keys not already in file
    for key, value in env.items():
        if key not in written and value:
            output.append(f'{key}={value}')

    _ENV_FILE.write_text("\n".join(output) + "\n")


def _reload_config() -> None:
    """Reload config module and re-export updated values into this module's globals."""
    import config as cfg
    importlib.reload(cfg)
    # Refresh env vars in os.environ from reloaded config
    mapping = {
        "FINNHUB_API_KEY":            getattr(cfg, "FINNHUB_API_KEY", ""),
        "FRED_API_KEY":               getattr(cfg, "FRED_API_KEY", ""),
        "NASDAQ_DATA_LINK_API_KEY":   getattr(cfg, "NASDAQ_DATA_LINK_API_KEY", ""),
        "POLY_PRIVATE_KEY":           getattr(cfg, "POLY_PRIVATE_KEY", ""),
        "POLY_FUNDER":                getattr(cfg, "POLY_FUNDER", ""),
        "POLY_HOST":                  getattr(cfg, "POLY_HOST", ""),
        "FINANCIAL_DATASETS_API_KEY": getattr(cfg, "FINANCIAL_DATASETS_API_KEY", ""),
        "OPENAI_API_KEY":             getattr(cfg, "OPENAI_API_KEY", ""),
        "ANTHROPIC_API_KEY":          getattr(cfg, "ANTHROPIC_API_KEY", ""),
        "GROQ_API_KEY":               getattr(cfg, "GROQ_API_KEY", ""),
        "DEEPSEEK_API_KEY":           getattr(cfg, "DEEPSEEK_API_KEY", ""),
        "EQUITY_MODEL_NAME":          getattr(cfg, "EQUITY_MODEL_NAME", ""),
        "EQUITY_MODEL_PROVIDER":      getattr(cfg, "EQUITY_MODEL_PROVIDER", ""),
        "FUNDA_API_KEY":              getattr(cfg, "FUNDA_API_KEY", ""),
        "ADANOS_API_KEY":             getattr(cfg, "ADANOS_API_KEY", ""),
    }
    for k, v in mapping.items():
        if v:
            os.environ[k] = v


@app.get("/api/settings")
async def get_settings():
    """
    Return all configurable keys with their current status.
    Sensitive keys return is_set=True/False and a masked preview — never the raw value.
    Non-sensitive keys (addresses, model names) return the actual value.
    """
    env = _read_env_file()
    # Also check os.environ as fallback (keys may have been set at shell level)
    combined = {k: (os.environ.get(k, "") or env.get(k, "")) for k in _KEY_DEFS}

    result: dict[str, dict] = {}
    for key, meta in _KEY_DEFS.items():
        val = combined.get(key, "")
        is_set = bool(val)
        if meta["sensitive"]:
            display_value = f"{val[:4]}…{val[-4:]}" if len(val) > 10 else ("••••••••" if is_set else "")
        else:
            display_value = val
        result[key] = {
            "group":         meta["group"],
            "label":         meta["label"],
            "sensitive":     meta["sensitive"],
            "placeholder":   meta["placeholder"],
            "docs_url":      meta["docs_url"],
            "is_set":        is_set,
            "display_value": display_value,
        }
    # Detect Railway cloud environment — .env writes won't persist across restarts
    on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_SERVICE_NAME"))

    return {
        "keys":        result,
        "env_file":    str(_ENV_FILE),
        "on_railway":  on_railway,
        "railway_note": (
            "Running on Railway: keys saved here persist until the next redeploy. "
            "For permanent storage add them in Railway Dashboard → Variables."
        ) if on_railway else None,
        "timestamp":   int(time.time()),
    }


@app.post("/api/settings")
async def save_settings(payload: Dict[str, str]):
    """
    Save one or more API keys to the .env file.
    Only keys listed in _KEY_DEFS are accepted. Empty-string values are ignored
    (they won't overwrite an existing key). To clear a key, use DELETE /api/settings/{key}.

    After writing, config is reloaded so the running server picks up the new values
    immediately — no restart required.
    """
    unknown = [k for k in payload if k not in _KEY_DEFS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown keys: {unknown}")

    env = _read_env_file()
    updated: list[str] = []
    for key, value in payload.items():
        value = value.strip()
        if value:
            env[key] = value
            updated.append(key)

    if updated:
        _write_env_file(env)
        _reload_config()

    return {
        "ok":      True,
        "updated": updated,
        "message": f"Saved {len(updated)} key(s). Changes are live immediately.",
    }


@app.delete("/api/settings/{key}")
async def delete_setting(key: str):
    """Remove a key from the .env file."""
    if key not in _KEY_DEFS:
        raise HTTPException(status_code=400, detail=f"Unknown key: {key}")
    env = _read_env_file()
    if key in env:
        del env[key]
        _write_env_file(env)
        _reload_config()
        return {"ok": True, "message": f"{key} removed."}
    return {"ok": True, "message": f"{key} was not set."}


@app.get("/api/settings/status")
async def settings_system_status():
    """
    Quick health-check for all three subsystems based on currently configured keys.
    Used by the Settings page to show a launch readiness checklist.
    """
    env = {k: (os.environ.get(k, "") or "") for k in _KEY_DEFS}

    poly_ready   = bool(env.get("POLY_PRIVATE_KEY") and env.get("POLY_FUNDER"))
    eq_data_ready = bool(env.get("FINANCIAL_DATASETS_API_KEY"))
    eq_llm_ready  = any(env.get(k) for k in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY"
    ))

    equity_status_detail = _equity_status()

    return {
        "paper_trading": {
            "ready":   True,
            "label":   "Paper Trading & Backtester",
            "message": "Always available — no keys required",
        },
        "polymarket_live": {
            "ready":   poly_ready,
            "label":   "Polymarket Live Trading",
            "message": "Ready" if poly_ready else "Requires POLY_PRIVATE_KEY + POLY_FUNDER",
        },
        "equity_ai": {
            "ready":   equity_status_detail["available"],
            "label":   "Equity AI Analyst Engine",
            "message": (
                f"Ready — {equity_status_detail['model_name']} via {equity_status_detail['model_provider']}"
                if equity_status_detail["available"]
                else "; ".join(equity_status_detail.get("warnings", ["Not configured"]))
            ),
        },
        "market_data": {
            "finnhub":  bool(env.get("FINNHUB_API_KEY")),
            "fred":     bool(env.get("FRED_API_KEY")),
            "nasdaq":   bool(env.get("NASDAQ_DATA_LINK_API_KEY")),
        },
        "timestamp": int(time.time()),
    }
