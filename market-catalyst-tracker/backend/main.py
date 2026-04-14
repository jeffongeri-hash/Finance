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
from analysis.prediction_scanner import (
    pull_all_prediction_data,
    get_biotech_fda_markets,
    get_macro_markets,
    get_geopolitical_markets,
    get_top_markets_all,
    enrich_catalysts_with_predictions,
    get_market_chart_data,
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
    period: str = Query("3mo", pattern=r"^(\d+[dmyDMY]|1mo|3mo|6mo|1y|2y|5y|max)$"),
    interval: str = Query("1d", pattern=r"^(1m|5m|15m|30m|1h|1d|1wk|1mo)$"),
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
