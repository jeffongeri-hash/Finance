# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: Market Catalyst Tracker

A real-time market intelligence and automated trading platform. **Backend**: Python FastAPI (async). **Frontend**: Vanilla JS + TradingView Lightweight Charts (no build step). **Trading**: IBKR Client Portal Gateway for live equities; Polymarket CLOB for prediction markets.

---

## Development Commands

```bash
# Start the backend (serves frontend too)
cd market-catalyst-tracker/backend
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
# App available at http://localhost:8000/

# Frontend — no build step. Browser loads ES6 modules directly from /static/js/
```

The frontend is pure static HTML/CSS/ES6 modules — no npm, no bundler, no compilation. Editing `.js` files is immediately reflected on page refresh.

### IBKR Gateway (for live trading)
```bash
# Download Client Portal Gateway from IBKR, then:
cd clientportal
./bin/run.sh root/conf.yaml
# Opens at https://localhost:5055 — auth once in browser
# Backend talks to it at IBKR_GATEWAY_URL (default: https://localhost:5055)
```

---

## Architecture

### Data Flow
```
External APIs → backend/data/*_adapter.py
             → backend/analysis/*.py  (scoring/scanning)
             → backend/main.py        (FastAPI routes + WebSocket)
             → frontend/static/js/*.js (fetch + DOM render)
```

### Backend Modules

**`backend/main.py`** — The entire FastAPI app in one file. All HTTP routes and the WebSocket handler live here. Routes are grouped by prefix: `/api/market/*`, `/api/charts/*`, `/api/news/*`, `/api/catalysts/*`, `/api/momentum/*`, `/api/predictions/*`, `/api/trading/*`, `/api/research/*`, `/api/ibkr/*`, `/api/journal/*`, `/api/equity/*`, `/api/settings/*`.

**`backend/config.py`** — All environment variables, market universe lists (S&P 100, biotech watchlist, etc.), timeouts, cache TTLs. The Settings page (`/api/settings` POST) writes to `.env` and reloads this module live — no restart needed.

**`backend/data/`** — One adapter per external data source. Each is a thin client that handles auth, TTL caching, and graceful failure. Key adapters:
- `yfinance_adapter.py` — Primary free source. Uses `fast_info` (not `t.info`) for performance — `fast_info` takes ~0.3s vs 10-30s for `t.info`. Use `t.history(period="2d")` for OHLCV only.
- `adanos_adapter.py` — Multi-source social sentiment (Reddit, X, news, Polymarket). Returns `buzz_score`, `bullish_pct`, `sentiment` label, `sources_agree`.
- `funda_adapter.py` — Funda AI: earnings calendar, fundamentals, options flow, SEC filings, congressional trades.
- `finnhub_adapter.py`, `fred_adapter.py`, `nasdaq_adapter.py` — Optional premium sources.

**`backend/analysis/`** — Scanning and scoring logic:
- `momentum_scanner.py` — Runs concurrent yfinance calls (8 workers) across the universe. Min score 10, min RVOL 1.2, 90s timeout. Produces ranked `MomentumCandidate` list.
- `catalyst_scanner.py` — FDA/SEC/ClinicalTrials biotech event detection.
- `news_correlator.py` — Determines *why* a ticker is moving; returns sentiment + news driver tag.
- `technical.py` — Multi-timeframe analysis, support/resistance, volatility profile.

**`backend/trading/`** — Trading logic:
- `strategy_engine.py` — **Entry**: composite signal requires score ≥ 25, RVOL ≥ 1.5, RSI ≤ 72, price > 20-day MA, no confirmed bearish Adanos sentiment. **Exit**: stop −6%, target +18%, trailing stop −7% from peak (activates after +10% gain), time stop 7 days. **Sizing**: 1% risk per trade, 7% max concentration, 10 max open positions.
- `trade_journal.py` — SQLite at `backend/trade_journal.db`. Records all entry signals + exit P&L. `signal_accuracy` table rolls win-rate per signal type for learning. Call `get_performance_stats()` for dashboard data.
- `ibkr_adapter.py` — Client Portal Gateway REST client. All calls go to `IBKR_GATEWAY_URL`. Session kept alive via `/tickle` (60s interval). `place_order()` handles IBKR's confirmation reply automatically.
- `engine.py` + `paper_trader.py` + `live_trader.py` — Polymarket trading engine (separate from IBKR equities).

### Frontend Modules

All in `frontend/static/js/`. Each file handles one view tab. They import from `api.js` and export an `init*()` and `load*()` function that `index.html` calls.

- `api.js` — Base fetch wrapper (`apiFetch`), WebSocket factory (`createPriceStream`), and `Fmt` helper (`.pct()`, `.price()`, `.currency()`, `.bigNum()`, `.timeAgo()`).
- `ibkr.js` — IBKR trading tab: gateway status bar, portfolio cards, positions/orders/history/performance/signals. Auto-refreshes every 30s; tickles gateway every 60s.
- `dashboard.js`, `momentum.js`, `catalyst.js`, `predictions.js`, `equity.js`, `technical.js`, `trading.js`, `settings.js` — One file per view section.

**Adding a new view**: add a `<section id="view-foo">` to `index.html`, a nav item, `import { initFoo, loadFoo } from "/static/js/foo.js"`, and a `case "foo": loadFoo(); break;` in the nav switch.

### WebSocket (live prices)
Backend: `ws://localhost:8000/ws/prices`. Send `{"action": "subscribe", "symbols": ["AAPL"]}`. Frontend uses `createPriceStream()` from `api.js`.

### Caching Pattern
`main.py` has a module-level `_cache: dict`. Heavy endpoints check `_cache.get(key)` and store results with timestamps. Quote TTL: 2 min. Scans: 10 min. News: 5 min.

---

## Environment Variables

Set via `.env` file or the in-app Settings page (which writes `.env` and hot-reloads `config.py`).

| Variable | Purpose | Required |
|---|---|---|
| `FUNDA_API_KEY` | Funda AI (earnings, fundamentals, options flow) | Optional |
| `ADANOS_API_KEY` | Multi-source social sentiment | Optional |
| `IBKR_GATEWAY_URL` | IBKR Client Portal Gateway URL | For live trading |
| `FINANCIAL_DATASETS_API_KEY` | Equity AI analyst agents | For Equity AI tab |
| `FINNHUB_API_KEY` | Recommendation trends, peers | Optional |
| `FRED_API_KEY` | Federal Reserve macro data | Optional |
| `NASDAQ_DATA_LINK_API_KEY` | Short interest, EOD history | Optional |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GROQ_API_KEY` | LLM for Equity AI | One required for Equity AI |
| `POLY_PRIVATE_KEY` + `POLY_FUNDER` | Live Polymarket trading | For live prediction markets |

yfinance, FDA API, SEC EDGAR, and ClinicalTrials.gov need no keys.

---

## Key Design Decisions

- **`fast_info` over `t.info`**: Always use `yfinance Ticker.fast_info` for price/volume. `t.info` triggers a full page scrape (10–30s); `fast_info` is ~0.3s. For 52-week high use `fast_info.year_high`.
- **Graceful degradation**: All data adapters catch exceptions and return empty dicts/lists. The app works with zero API keys (yfinance only).
- **No frontend build toolchain**: Keep it that way. Avoid introducing npm/webpack unless absolutely necessary.
- **Single `main.py`**: All routes stay in `main.py`. New endpoints follow the existing pattern: define a Pydantic response model in `models/schemas.py`, add the route, use `asyncio.get_event_loop().run_in_executor()` for blocking I/O.
