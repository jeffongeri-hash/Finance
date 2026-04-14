"""
Polymarket Trading Engine
==========================
Orchestrates the market lifecycle: PENDING → ACTIVE → RESOLVING → RESOLVED.
Inspired by the "early-bird" architecture described in the article:

  "The idea was to always start in a future market slot, at least one ahead
   of the current one. Every market is a lifecycle: it goes through
   start → run → end states."

Each market lifecycle:
  1. start()   — discover next market slot, initialise context, price data
  2. run()     — execute strategy (signal detection → order placement)
  3. end()     — resolve position (sell early or hold to resolution), record PnL

The engine is fully async and runs paper trading by default.
When live_mode=True and a wallet private_key is provided, it will submit
real orders to the Polymarket CLOB (not yet implemented — infrastructure
is in place for when you provide the API key).

Risk controls:
  • max_position_pct — max % of balance per position (default 5%)
  • stop_loss_pct    — auto-close if position loses > N% (default 15%)
  • daily_loss_limit — halt engine if daily PnL < -N$ (default 500$)
  • min_confidence   — minimum signal confidence to enter (default 0.55)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from data.polymarket_adapter import (
    get_top_markets, get_markets_by_tag, get_live_midpoint,
    search_markets, enrich_with_live_price,
)
from analysis.prediction_scanner import get_biotech_fda_markets, get_macro_markets
from analysis.news_correlator import correlate_market_news
from analysis.momentum_scanner import run_momentum_scan
from trading.models import (
    MarketContext, MarketPhase, OrderSide, EngineState, SignalType,
)
from trading.paper_trader import PaperTrader
from trading.signals import run_signal_scan

logger = logging.getLogger(__name__)


class TradingEngine:
    """
    Core trading engine. Runs paper mode by default.

    Usage:
        engine = TradingEngine(initial_balance=10_000)
        await engine.start()
        # engine runs autonomously in background
        stats = engine.get_stats()
        await engine.stop()
    """

    def __init__(
        self,
        initial_balance:    float = 10_000.0,
        max_position_pct:   float = 0.05,    # 5% of balance per position
        stop_loss_pct:      float = 0.15,    # 15% loss triggers auto-close
        daily_loss_limit:   float = 500.0,   # halt if daily PnL < -$500
        min_confidence:     float = 0.55,    # signal confidence threshold
        scan_interval_s:    int   = 60,      # re-scan every 60s
        settlement_poll_s:  int   = 5,       # check settlement every 5s
        live_mode:          bool  = False,   # False = paper trading only
        private_key:        str   = "",      # for live mode (not yet active)
    ):
        self.trader           = PaperTrader(initial_balance)
        self.max_position_pct = max_position_pct
        self.stop_loss_pct    = stop_loss_pct
        self.daily_loss_limit = daily_loss_limit
        self.min_confidence   = min_confidence
        self.scan_interval_s  = scan_interval_s
        self.settlement_poll_s= settlement_poll_s
        self.live_mode        = live_mode

        self._running  = False
        self._tasks:   List[asyncio.Task] = []
        self._markets: Dict[str, MarketContext] = {}
        self._signals: List[Dict] = []
        self._log:     List[Dict] = []   # engine event log (last 200)
        self._day_start_balance = initial_balance
        self._day_start_ts      = time.time()

    # ── Control ────────────────────────────────────────────────────────────────

    async def start(self):
        """Start the engine background loops."""
        if self._running:
            return
        self._running = True
        self._log_event("ENGINE_START", f"Balance: ${self.trader.balance:,.2f} | Mode: {'LIVE' if self.live_mode else 'PAPER'}")

        self._tasks = [
            asyncio.create_task(self._scan_loop()),
            asyncio.create_task(self._settlement_loop()),
            asyncio.create_task(self._stop_loss_monitor()),
            asyncio.create_task(self._daily_reset_loop()),
        ]

    async def stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._log_event("ENGINE_STOP", f"Final balance: ${self.trader.balance:,.2f} | PnL: ${self.trader.state.total_pnl:,.2f}")

    # ── Main scan loop ─────────────────────────────────────────────────────────

    async def _scan_loop(self):
        """Periodically scan for signals and execute trades."""
        while self._running:
            try:
                await self._scan_and_trade()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Scan loop error: %s", e, exc_info=True)
            await asyncio.sleep(self.scan_interval_s)

    async def _scan_and_trade(self):
        """One scan-and-trade cycle."""
        # Daily loss circuit breaker
        if self._daily_pnl() < -self.daily_loss_limit:
            self._log_event("CIRCUIT_BREAKER", f"Daily loss limit hit: ${self._daily_pnl():,.2f}")
            return

        # Fetch fresh market data
        biotech   = await get_biotech_fda_markets(20)
        macro     = await get_macro_markets(20)
        news_corr = await asyncio.get_event_loop().run_in_executor(
            None, lambda: correlate_market_news("SPY")
        )
        news_items = [n.model_dump() for n in news_corr.supporting_news[:10]]

        # Momentum scan (lightweight — top 20 names only)
        momentum_result = await run_momentum_scan(universe=None, top_n=20, min_score=35)
        momentum_cands  = [c.model_dump() for c in momentum_result.candidates]

        # Run all signal detectors
        signals = await run_signal_scan(
            biotech_markets     = biotech,
            macro_markets       = macro,
            news_items          = news_items,
            momentum_candidates = momentum_cands,
        )

        self._signals = signals
        self._log_event("SCAN_COMPLETE", f"{len(signals)} signals found")

        # Execute top signals above confidence threshold
        for sig in signals:
            if sig["confidence"] < self.min_confidence:
                continue
            if not sig["market_id"]:
                continue
            await self._execute_signal(sig, biotech + macro)

    async def _execute_signal(self, signal: Dict, markets: List[Dict]):
        """Attempt to enter a position based on a signal."""
        market_id = signal["market_id"]
        side_str  = signal["side"]
        side      = OrderSide.YES if side_str == "yes" else OrderSide.NO

        # Don't double-enter the same market+side
        if self.trader.get_position(market_id, side):
            return

        # Find the raw market data
        market_data = next((m for m in markets if m.get("condition_id") == market_id), None)
        if not market_data:
            return

        # Enrich with live price
        market_data = await enrich_with_live_price(market_data)
        yes_p = market_data.get("yes_price") or 0.5
        no_p  = market_data.get("no_price")  or (1 - yes_p)

        # Build MarketContext
        ctx = MarketContext(
            condition_id  = market_id,
            question      = market_data.get("question", ""),
            slug          = market_data.get("slug", ""),
            yes_token_id  = market_data.get("yes_token_id") or "",
            phase         = MarketPhase.ACTIVE,
            yes_price     = yes_p,
            no_price      = no_p,
            volume        = market_data.get("volume", 0),
            liquidity     = market_data.get("liquidity", 0),
        )

        # Position size: max_position_pct of current balance
        size = min(
            self.trader.balance * self.max_position_pct,
            signal.get("suggested_size") or self.trader.balance * self.max_position_pct,
            self.trader.balance * 0.10,  # hard cap at 10%
        )

        if size < 5:  # minimum $5 trade
            return

        # Limit price = suggested price or current mid
        limit_price = signal.get("suggested_price") or (yes_p if side == OrderSide.YES else no_p)
        limit_price = max(0.02, min(0.98, limit_price))

        order = await self.trader.buy(ctx, side, size, limit_price)
        if order:
            self._markets[market_id] = ctx
            self._log_event("TRADE_OPEN", (
                f"{signal['signal_type'].upper()} | {side.value.upper()} {market_id[:12]}… | "
                f"${size:.0f} @ {limit_price:.3f} | conf={signal['confidence']:.0%} | "
                f"{signal['rationale'][:60]}"
            ))

    # ── Stop-loss monitor ──────────────────────────────────────────────────────

    async def _stop_loss_monitor(self):
        """Continuously check open positions for stop-loss breaches."""
        while self._running:
            try:
                await asyncio.sleep(10)
                for pos in list(self.trader.positions):
                    if not pos.settled:
                        continue
                    ctx = self._markets.get(pos.market_id)
                    if not ctx:
                        continue
                    # Get live price
                    current = await get_live_midpoint(ctx.yes_token_id)
                    if current is None:
                        continue
                    current_p = current if pos.side == OrderSide.YES else (1 - current)
                    unrealized_loss = (current_p - pos.avg_price) / pos.avg_price
                    if unrealized_loss < -self.stop_loss_pct:
                        # Exit: sell at best available price
                        trade = await self.trader.sell(ctx, pos, min_price=current_p * 0.95)
                        if trade:
                            self._log_event("STOP_LOSS",
                                f"{pos.market_id[:12]}… loss={unrealized_loss:.1%} → closed @ {current_p:.3f}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Stop-loss monitor: %s", e)

    # ── Settlement loop ────────────────────────────────────────────────────────

    async def _settlement_loop(self):
        """Promote pending orders to confirmed as simulated block time passes."""
        while self._running:
            try:
                await asyncio.sleep(self.settlement_poll_s)
                await self.trader.process_settlements()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Settlement loop: %s", e)

    # ── Daily reset ────────────────────────────────────────────────────────────

    async def _daily_reset_loop(self):
        """Reset daily PnL baseline at midnight UTC."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # check every hour
                now = time.time()
                if now - self._day_start_ts >= 86400:
                    self._day_start_balance = self.trader.balance
                    self._day_start_ts      = now
                    self._log_event("DAILY_RESET", f"New baseline: ${self._day_start_balance:,.2f}")
            except asyncio.CancelledError:
                break

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _daily_pnl(self) -> float:
        return self.trader.balance - self._day_start_balance

    def _log_event(self, event: str, detail: str):
        entry = {"ts": int(time.time()), "event": event, "detail": detail}
        self._log.append(entry)
        if len(self._log) > 200:
            self._log = self._log[-200:]
        logger.info("[%s] %s", event, detail)

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        return {
            **self.trader.stats(),
            "is_running":    self._running,
            "live_mode":     self.live_mode,
            "daily_pnl":     round(self._daily_pnl(), 2),
            "active_signals":len(self._signals),
            "signals":       self._signals[:10],  # top 10 current signals
            "event_log":     self._log[-30:],      # last 30 events
            "open_markets":  list(self._markets.keys()),
        }

    def get_signals(self) -> List[Dict]:
        return self._signals

    def get_log(self) -> List[Dict]:
        return self._log


# ── Singleton engine instance (shared across API routes) ─────────────────────

_engine_instance: Optional[TradingEngine] = None


def get_engine() -> Optional[TradingEngine]:
    return _engine_instance


def create_engine(initial_balance: float = 10_000.0, **kwargs) -> TradingEngine:
    global _engine_instance
    _engine_instance = TradingEngine(initial_balance=initial_balance, **kwargs)
    return _engine_instance
