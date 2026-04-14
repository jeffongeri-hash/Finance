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
from trading.live_trader import LiveTrader, create_live_trader
from trading.signals import run_signal_scan
from trading.penny_scanner import (
    scan_penny_markets, rank_opportunities, validate_portfolio_ev,
    TAKE_PROFIT as PENNY_TP, TARGET_POS as PENNY_TARGET_POS,
    ORDER_SIZE as PENNY_ORDER_SIZE,
)

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
        private_key:        str   = "",      # Polygon wallet private key
        funder:             str   = "",      # Polymarket funder address
        poly_host:          str   = "https://clob.polymarket.com",
    ):
        # Paper trader always active (used as shadow in live mode)
        self._paper = PaperTrader(initial_balance)
        self._live:  Optional[LiveTrader] = None

        # Active trader is paper by default; switch to live via enable_live_mode()
        self.trader           = self._paper
        self.live_mode        = False
        self._live_error:     Optional[str] = None  # last live-mode init error

        self.max_position_pct = max_position_pct
        self.stop_loss_pct    = stop_loss_pct
        self.daily_loss_limit = daily_loss_limit
        self.min_confidence   = min_confidence
        self.scan_interval_s  = scan_interval_s
        self.settlement_poll_s= settlement_poll_s

        self._running  = False
        self._tasks:   List[asyncio.Task] = []
        self._markets: Dict[str, MarketContext] = {}
        self._signals: List[Dict] = []
        self._log:     List[Dict] = []   # engine event log (last 200)
        self._day_start_balance = initial_balance
        self._day_start_ts      = time.time()

        # Penny harvest tracking
        self._penny_positions: Dict[str, Dict] = {}  # token_id → opportunity metadata
        self._penny_scan_ts:   float = 0.0

        # Auto-enable live mode if credentials were passed at construction
        if live_mode and private_key and funder:
            err = self._try_init_live(private_key, funder, poly_host)
            if err:
                self._live_error = err
                logger.warning("Engine started in PAPER mode (live init failed): %s", err)

    # ── Live / Paper mode management ───────────────────────────────────────────

    def _try_init_live(self, private_key: str, funder: str, host: str) -> Optional[str]:
        """
        Attempt to create a LiveTrader. Returns None on success, error string on failure.
        Does NOT switch self.trader — caller decides whether to switch.
        """
        lt = create_live_trader(private_key, funder, host)
        if lt is None:
            return "py-clob-client not installed or credentials missing"
        self._live = lt
        return None

    def enable_live_mode(
        self,
        private_key: str,
        funder: str,
        host: str = "https://clob.polymarket.com",
    ) -> Dict:
        """
        Switch the active trader to LiveTrader.
        Returns {"ok": True} or {"ok": False, "error": "..."}.
        """
        if not private_key or not funder:
            return {"ok": False, "error": "POLY_PRIVATE_KEY and POLY_FUNDER are required"}

        # Re-init live trader (allows rotating credentials at runtime)
        err = self._try_init_live(private_key, funder, host)
        if err:
            self._live_error = err
            return {"ok": False, "error": err}

        self.trader    = self._live
        self.live_mode = True
        self._live_error = None
        self._log_event(
            "LIVE_MODE_ENABLED",
            f"Switched to LiveTrader | funder={funder[:10]}… | host={host}"
        )
        logger.info("TradingEngine switched to LIVE mode")
        return {"ok": True, "funder_prefix": funder[:10]}

    def disable_live_mode(self) -> Dict:
        """
        Switch back to PaperTrader. The LiveTrader is kept in self._live
        so it can be re-enabled without re-authenticating.
        """
        self.trader    = self._paper
        self.live_mode = False
        self._log_event("PAPER_MODE_ENABLED", "Switched back to PaperTrader")
        logger.info("TradingEngine switched to PAPER mode")
        return {"ok": True}

    async def live_health_check(self) -> Dict:
        """CLOB connectivity probe. Returns health dict from LiveTrader."""
        if self._live is None:
            return {
                "connected":     False,
                "note":          "LiveTrader not initialised — set POLY credentials first",
                "error":         self._live_error,
            }
        return await self._live.health_check()

    # ── Control ────────────────────────────────────────────────────────────────

    async def start(self):
        """Start the engine background loops."""
        if self._running:
            return
        self._running = True
        mode_str = "LIVE" if self.live_mode else "PAPER"
        self._log_event(
            "ENGINE_START",
            f"Balance: ${self.trader.balance:,.2f} | Mode: {mode_str}"
        )

        self._tasks = [
            asyncio.create_task(self._scan_loop()),
            asyncio.create_task(self._penny_harvest_loop()),
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

        # Run all signal detectors (pass penny position count for harvest slots)
        penny_pos_count = len(self._penny_positions)
        signals = await run_signal_scan(
            biotech_markets          = biotech,
            macro_markets            = macro,
            news_items               = news_items,
            momentum_candidates      = momentum_cands,
            current_penny_positions  = penny_pos_count,
        )

        self._signals = signals
        penny_count = sum(1 for s in signals if s.get("signal_type") == "penny_harvest")
        self._log_event(
            "SCAN_COMPLETE",
            f"{len(signals)} signals ({penny_count} penny harvest) | "
            f"penny positions: {penny_pos_count}/{PENNY_TARGET_POS}"
        )

        # Execute top signals above confidence threshold
        # NOTE: penny_harvest signals bypass min_confidence check — edge is in EV not per-trade probability
        for sig in signals:
            if sig.get("signal_type") == "penny_harvest":
                await self._execute_penny_signal(sig)
            elif sig["confidence"] >= self.min_confidence and sig["market_id"]:
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

    # ── Penny harvest execution ───────────────────────────────────────────────

    async def _execute_penny_signal(self, signal: Dict):
        """
        Execute a PENNY_HARVEST signal with special rules:
          - Position size: exactly $1 (100 shares @ $0.01)
          - Limit order at entry_price (no taker fee)
          - Take profit pre-set at 99c
          - NO stop loss (loss is already capped at $1)
          - Never double-enter same token
        """
        token_id  = signal.get("metadata", {}).get("token_id", "")
        market_id = signal["market_id"]

        if not token_id or not market_id:
            return

        # Never double-enter same token
        if token_id in self._penny_positions:
            return

        # Enforce position cap
        if len(self._penny_positions) >= PENNY_TARGET_POS:
            return

        side_str  = signal["side"]
        side      = OrderSide.YES if side_str == "yes" else OrderSide.NO
        entry_p   = signal.get("suggested_price") or 0.01
        meta      = signal.get("metadata", {})

        ctx = MarketContext(
            condition_id = market_id,
            question     = meta.get("question", "")[:80],
            slug         = meta.get("slug", ""),
            yes_token_id = token_id if side == OrderSide.YES else "",
            no_token_id  = token_id if side == OrderSide.NO  else "",
            phase        = MarketPhase.ACTIVE,
            yes_price    = entry_p if side == OrderSide.YES else 1 - entry_p,
            no_price     = entry_p if side == OrderSide.NO  else 1 - entry_p,
        )

        # $1 position: 100 shares × $0.01
        dollar_size = min(1.0, self.trader.balance * 0.001)  # never risk more than 0.1% balance
        if dollar_size < 0.50:
            return

        order = await self.trader.buy(ctx, side, dollar_size, entry_p)
        if order:
            self._penny_positions[token_id] = {
                "market_id":   market_id,
                "token_id":    token_id,
                "side":        side_str,
                "entry_price": entry_p,
                "take_profit": PENNY_TP,
                "ev":          meta.get("ev", 0),
                "question":    meta.get("question", "")[:60],
                "slug":        meta.get("slug", ""),
                "entered_at":  time.time(),
            }
            self._markets[market_id] = ctx
            self._log_event("PENNY_ENTRY",
                f"${dollar_size:.2f} @ {entry_p:.3f} | "
                f"EV=+${meta.get('ev', 0):.4f} | "
                f"TP={PENNY_TP} | {meta.get('question', '')[:50]}")

    async def _penny_harvest_loop(self):
        """
        Dedicated penny harvest monitor loop.
        - Scans for new opportunities every 15 minutes
        - Monitors existing positions and places TP orders when price hits 99c
        - Never sets stop-loss (the edge requires holding until resolution)
        """
        SCAN_INTERVAL = 900   # 15 minutes
        MONITOR_INTERVAL = 30  # check prices every 30s

        monitor_tick = 0
        while self._running:
            try:
                await asyncio.sleep(MONITOR_INTERVAL)
                monitor_tick += 1

                # Check for TP on existing positions every 30s
                await self._check_penny_take_profits()

                # Re-scan for new opportunities every 15 min
                if monitor_tick * MONITOR_INTERVAL >= SCAN_INTERVAL:
                    monitor_tick = 0
                    open_count = len(self._penny_positions)
                    self._log_event(
                        "PENNY_SCAN",
                        f"Open: {open_count}/{PENNY_TARGET_POS} | "
                        f"balance: ${self.trader.balance:,.2f}"
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Penny harvest loop: %s", e)

    async def _check_penny_take_profits(self):
        """Check all open penny positions and take profit at PENNY_TP."""
        from data.polymarket_adapter import get_live_midpoint

        for token_id, pos_meta in list(self._penny_positions.items()):
            try:
                current = await get_live_midpoint(token_id)
                if current is None:
                    continue

                # Take profit: price has reached 99c
                if current >= PENNY_TP * 0.98:  # 0.97+ triggers (slight tolerance)
                    market_id = pos_meta["market_id"]
                    ctx = self._markets.get(market_id)
                    if not ctx:
                        continue
                    side = OrderSide.YES if pos_meta["side"] == "yes" else OrderSide.NO
                    position = self.trader.get_position(market_id, side)
                    if position and position.settled:
                        trade = await self.trader.sell(ctx, position, min_price=current * 0.95)
                        if trade:
                            hold_s = time.time() - pos_meta["entered_at"]
                            self._log_event("PENNY_TP",
                                f"${trade.net_pnl:+.2f} PnL | "
                                f"entry={pos_meta['entry_price']:.3f} exit={current:.3f} | "
                                f"held {hold_s/3600:.1f}h | {pos_meta['question'][:40]}")
                            del self._penny_positions[token_id]

                # Resolved at zero — remove from tracking
                elif current <= 0.01 and pos_meta.get("entry_price", 0.01) > 0.005:
                    # Price essentially zero → resolved as loss, clean up
                    del self._penny_positions[token_id]
                    self._log_event("PENNY_LOSS",
                        f"resolved @ {current:.3f} | {pos_meta['question'][:50]}")

            except Exception as e:
                logger.debug("Penny TP check %s: %s", token_id[:12], e)

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
        penny_pos = list(self._penny_positions.values())

        # Paper shadow stats (always available for comparison)
        paper_stats = self._paper.stats() if self.live_mode else {}

        # Pending CLOB orders (live mode only)
        pending_orders: List[Dict] = []
        if self.live_mode and self._live:
            pending_orders = self._live.get_pending_orders()

        return {
            **self.trader.stats(),
            "is_running":            self._running,
            "live_mode":             self.live_mode,
            "live_available":        self._live is not None,
            "live_error":            self._live_error,
            "daily_pnl":             round(self._daily_pnl(), 2),
            "active_signals":        len(self._signals),
            "signals":               self._signals[:10],
            "event_log":             self._log[-30:],
            "open_markets":          list(self._markets.keys()),
            "penny_positions":       penny_pos,
            "penny_count":           len(penny_pos),
            "penny_target":          PENNY_TARGET_POS,
            "penny_capital_at_risk": round(sum(p.get("entry_price", 0.01) for p in penny_pos), 2),
            "pending_live_orders":   pending_orders,
            "paper_shadow":          paper_stats if self.live_mode else None,
        }

    def get_penny_positions(self) -> List[Dict]:
        return list(self._penny_positions.values())

    def get_signals(self) -> List[Dict]:
        return self._signals

    def get_log(self) -> List[Dict]:
        return self._log


# ── Singleton engine instance (shared across API routes) ─────────────────────

_engine_instance: Optional[TradingEngine] = None


def get_engine() -> Optional[TradingEngine]:
    return _engine_instance


def create_engine(
    initial_balance: float = 10_000.0,
    private_key:     str   = "",
    funder:          str   = "",
    poly_host:       str   = "https://clob.polymarket.com",
    **kwargs,
) -> TradingEngine:
    global _engine_instance
    _engine_instance = TradingEngine(
        initial_balance=initial_balance,
        private_key=private_key,
        funder=funder,
        poly_host=poly_host,
        **kwargs,
    )
    return _engine_instance
