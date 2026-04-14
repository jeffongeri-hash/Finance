"""
Prediction Market Backtester
=============================
Pure-Python replay engine for Polymarket strategies, ported from the
prediction-market-backtesting repo (github.com/evan-kolberg/prediction-market-backtesting).

Strategies ported (all Long-Only, same logic as NautilusTrader versions):
  - MeanReversion     — buy below rolling avg, exit on recovery
  - Breakout          — buy above mean + N*std, exit on mean reversion
  - PanicFade         — buy rapid drops below threshold, exit on rebound
  - ThresholdMomentum — buy on threshold cross, hold to TP/SL
  - EMACrossover      — fast/slow EMA cross entry/exit
  - VWAPReversion     — buy when price drops below VWAP by threshold
  - DeepValue         — buy price ≤ 0.25 and hold to resolution

Data source: Polymarket CLOB /prices-history/{token_id} (free, no key).
Fee model:   (20 bps / 10000) × min(p, 1-p) × shares  (exact Polymarket formula).
Slippage:    0.3% taker on entry, 0.1% maker-side spread on exit.

The BacktestRunner runs continuously on a background loop and populates
a shared in-memory results store consumed by /api/trading/backtest/*.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from data.polymarket_adapter import get_price_history, get_top_markets

logger = logging.getLogger(__name__)

# ── Fee / execution constants ─────────────────────────────────────────────────
FEE_BPS      = 20
TAKER_SLIP   = 0.003   # 0.30% taker slippage on entry
MAKER_SLIP   = 0.001   # 0.10% spread cost on exit
MIN_PRICE    = 0.02
MAX_PRICE    = 0.98


def _fee(price: float, shares: float) -> float:
    return (FEE_BPS / 10_000) * min(price, 1 - price) * shares


def _apply_taker(price: float) -> float:
    """Simulate taker fill: pay slightly above ask."""
    return min(MAX_PRICE, price * (1 + TAKER_SLIP))


def _apply_maker(price: float) -> float:
    """Simulate limit exit: receive slightly below bid."""
    return max(MIN_PRICE, price * (1 - MAKER_SLIP))


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    entry_price: float
    exit_price:  float
    shares:      float
    fee_paid:    float
    net_pnl:     float
    duration_bars: int


@dataclass
class BacktestResult:
    strategy:       str
    market_slug:    str
    token_id:       str
    bars_replayed:  int
    trades:         int
    win_rate:       float
    total_return:   float    # % return on initial capital
    sharpe:         float
    max_drawdown:   float
    avg_duration:   float    # bars
    profit_factor:  float
    expected_value: float    # avg net pnl per trade
    config:         Dict     = field(default_factory=dict)
    timestamp:      float    = field(default_factory=time.time)


# ── Strategy implementations (pure Python, no NautilusTrader) ─────────────────

class _LongOnlyBase:
    """Shared position state and trade logging."""

    def __init__(self, initial_cash: float = 100.0, trade_size: float = 5.0):
        self.cash        = initial_cash
        self.trade_size  = trade_size   # $ per trade
        self.in_position = False
        self.entry_price = 0.0
        self.entry_bar   = 0
        self.shares      = 0.0
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = [initial_cash]

    def _enter(self, price: float, bar: int) -> None:
        fill = _apply_taker(price)
        if fill <= 0 or self.cash < self.trade_size:
            return
        self.shares = self.trade_size / fill
        fee = _fee(fill, self.shares)
        self.cash -= (self.trade_size + fee)
        self.entry_price = fill
        self.entry_bar   = bar
        self.in_position = True

    def _exit(self, price: float, bar: int) -> None:
        if not self.in_position:
            return
        fill = _apply_maker(price)
        revenue = self.shares * fill
        fee = _fee(fill, self.shares)
        net = revenue - fee
        self.cash += net
        gross_pnl = revenue - self.trade_size
        net_pnl   = net - self.trade_size
        self.trades.append(BacktestTrade(
            entry_price  = self.entry_price,
            exit_price   = fill,
            shares       = self.shares,
            fee_paid     = fee,
            net_pnl      = net_pnl,
            duration_bars= bar - self.entry_bar,
        ))
        self.shares      = 0.0
        self.entry_price = 0.0
        self.in_position = False
        self.equity_curve.append(self.cash)

    def _mark(self, price: float) -> None:
        """Mark-to-market current equity for drawdown calculation."""
        if self.in_position:
            mtm = self.cash + self.shares * price
            self.equity_curve.append(mtm)
        else:
            self.equity_curve.append(self.cash)

    def run(self, prices: List[float]) -> None:
        raise NotImplementedError


class MeanReversionStrategy(_LongOnlyBase):
    def __init__(self, window=20, entry_threshold=0.01,
                 take_profit=0.05, stop_loss=0.03, **kwargs):
        super().__init__(**kwargs)
        self.window           = window
        self.entry_threshold  = entry_threshold
        self.take_profit      = take_profit
        self.stop_loss        = stop_loss

    def run(self, prices: List[float]) -> None:
        buf: deque = deque(maxlen=self.window)
        for i, p in enumerate(prices):
            buf.append(p)
            if not self.in_position:
                if len(buf) == self.window:
                    avg = sum(buf) / len(buf)
                    if p <= avg - self.entry_threshold:
                        self._enter(p, i)
            else:
                avg = sum(buf) / len(buf)
                tp_hit = p >= self.entry_price + self.take_profit
                sl_hit = self.stop_loss > 0 and p <= self.entry_price - self.stop_loss
                recover = p >= avg
                if tp_hit or sl_hit or recover:
                    self._exit(p, i)
            self._mark(p)


class BreakoutStrategy(_LongOnlyBase):
    def __init__(self, window=30, breakout_std=1.25,
                 max_entry_price=0.92, take_profit=0.02, stop_loss=0.02,
                 min_holding=0, cooldown=0, **kwargs):
        super().__init__(**kwargs)
        self.window           = window
        self.breakout_std     = breakout_std
        self.max_entry_price  = max_entry_price
        self.take_profit      = take_profit
        self.stop_loss        = stop_loss
        self.min_holding      = min_holding
        self.cooldown         = cooldown
        self._holding         = 0
        self._cooldown_rem    = 0
        self._prev            = None

    def run(self, prices: List[float]) -> None:
        buf: deque = deque(maxlen=self.window)
        for i, p in enumerate(prices):
            prev = self._prev
            buf.append(p)
            self._prev = p

            if not self.in_position:
                if self._cooldown_rem > 0:
                    self._cooldown_rem -= 1
                    self._mark(p); continue
                if len(buf) == self.window:
                    mean = sum(buf) / len(buf)
                    var  = sum((x - mean) ** 2 for x in buf) / len(buf)
                    std  = math.sqrt(var)
                    level = mean + self.breakout_std * std
                    crossed = prev is not None and prev < level
                    if p >= level and p <= self.max_entry_price and crossed:
                        self._enter(p, i)
                        self._holding = 0
            else:
                self._holding += 1
                if self.take_profit > 0 and p >= self.entry_price + self.take_profit:
                    self._exit(p, i); self._cooldown_rem = self.cooldown; self._mark(p); continue
                if self.stop_loss > 0 and p <= self.entry_price - self.stop_loss:
                    self._exit(p, i); self._cooldown_rem = self.cooldown; self._mark(p); continue
                if len(buf) == self.window and self._holding >= self.min_holding:
                    mean = sum(buf) / len(buf)
                    if p <= mean:
                        self._exit(p, i); self._cooldown_rem = self.cooldown; self._mark(p); continue
            self._mark(p)


class PanicFadeStrategy(_LongOnlyBase):
    def __init__(self, drop_window=80, min_drop=0.06, panic_price=0.30,
                 rebound_exit=0.42, max_holding=500,
                 take_profit=0.04, stop_loss=0.03, **kwargs):
        super().__init__(**kwargs)
        self.drop_window  = drop_window
        self.min_drop     = min_drop
        self.panic_price  = panic_price
        self.rebound_exit = rebound_exit
        self.max_holding  = max_holding
        self.take_profit  = take_profit
        self.stop_loss    = stop_loss
        self._holding     = 0

    def run(self, prices: List[float]) -> None:
        buf: deque = deque(maxlen=self.drop_window)
        for i, p in enumerate(prices):
            buf.append(p)
            if not self.in_position:
                if len(buf) == self.drop_window:
                    peak = max(buf)
                    drop = peak - p
                    if p <= self.panic_price and drop >= self.min_drop:
                        self._enter(p, i)
                        self._holding = 0
            else:
                self._holding += 1
                if self.take_profit > 0 and p >= self.entry_price + self.take_profit:
                    self._exit(p, i); self._mark(p); continue
                if self.stop_loss > 0 and p <= self.entry_price - self.stop_loss:
                    self._exit(p, i); self._mark(p); continue
                if p >= self.rebound_exit or self._holding >= self.max_holding:
                    self._exit(p, i); self._mark(p); continue
            self._mark(p)


class ThresholdMomentumStrategy(_LongOnlyBase):
    def __init__(self, entry_price=0.80, take_profit_price=0.92,
                 stop_loss_price=0.50, **kwargs):
        super().__init__(**kwargs)
        self.entry_p    = entry_price
        self.take_p     = take_profit_price
        self.stop_p     = stop_loss_price
        self._entered   = False
        self._prev      = None

    def run(self, prices: List[float]) -> None:
        for i, p in enumerate(prices):
            prev = self._prev
            self._prev = p
            if not self.in_position:
                if self._entered:
                    self._mark(p); continue
                crossed = prev is not None and prev < self.entry_p <= p
                if crossed:
                    self._enter(p, i)
                    self._entered = True
            else:
                if p >= self.take_p or p <= self.stop_p:
                    self._exit(p, i); self._mark(p); continue
            self._mark(p)


class EMACrossoverStrategy(_LongOnlyBase):
    def __init__(self, fast_period=20, slow_period=60,
                 entry_buffer=0.0, take_profit=0.0, stop_loss=0.0, **kwargs):
        super().__init__(**kwargs)
        self.fast_period  = fast_period
        self.slow_period  = slow_period
        self.entry_buffer = entry_buffer
        self.take_profit  = take_profit
        self.stop_loss    = stop_loss
        self._fast: Optional[float] = None
        self._slow: Optional[float] = None
        self._warmup = 0
        self._af = 2.0 / (fast_period + 1)
        self._as = 2.0 / (slow_period + 1)
        self._prev_fast: Optional[float] = None
        self._prev_slow: Optional[float] = None

    def run(self, prices: List[float]) -> None:
        needed = max(self.fast_period, self.slow_period)
        for i, p in enumerate(prices):
            pf, ps = self._fast, self._slow
            if pf is None:
                self._fast = p; self._slow = p; self._warmup = 1
                self._mark(p); continue
            self._fast = self._af * p + (1 - self._af) * self._fast
            self._slow = self._as * p + (1 - self._as) * self._slow
            self._warmup += 1
            if self._warmup < needed:
                self._mark(p); continue

            if not self.in_position:
                # Bullish cross: fast was below slow, now above (+ buffer)
                cross_up = (pf is not None and ps is not None
                            and pf <= ps
                            and self._fast > self._slow + self.entry_buffer)
                if cross_up:
                    self._enter(p, i)
            else:
                # Exit: fast crosses below slow
                cross_down = self._fast < self._slow
                tp_hit = self.take_profit > 0 and p >= self.entry_price + self.take_profit
                sl_hit = self.stop_loss > 0 and p <= self.entry_price - self.stop_loss
                if cross_down or tp_hit or sl_hit:
                    self._exit(p, i)
            pf_prev, ps_prev = self._fast, self._slow
            self._mark(p)


class VWAPReversionStrategy(_LongOnlyBase):
    def __init__(self, vwap_window=80, entry_threshold=0.008,
                 exit_threshold=0.002, take_profit=0.015, stop_loss=0.02, **kwargs):
        super().__init__(**kwargs)
        self.vwap_window      = vwap_window
        self.entry_threshold  = entry_threshold
        self.exit_threshold   = exit_threshold
        self.take_profit      = take_profit
        self.stop_loss        = stop_loss
        self._window: deque   = deque(maxlen=vwap_window)
        self._wsum            = 0.0
        self._ssum            = 0.0

    def _vwap(self) -> Optional[float]:
        if self._ssum <= 0 or not self._window:
            return None
        return self._wsum / self._ssum

    def run(self, prices: List[float]) -> None:
        for i, p in enumerate(prices):
            # VWAP with equal size=1 per bar (price-only data)
            if len(self._window) == self._window.maxlen:
                old_p, old_s = self._window[0], 1.0
                self._wsum -= old_p * old_s
                self._ssum -= old_s
            self._window.append(p)
            self._wsum += p
            self._ssum += 1.0

            vwap = self._vwap()
            if vwap is None or len(self._window) < self.vwap_window:
                self._mark(p); continue

            if not self.in_position:
                if p <= vwap - self.entry_threshold:
                    self._enter(p, i)
            else:
                tp_hit = self.take_profit > 0 and p >= self.entry_price + self.take_profit
                sl_hit = self.stop_loss > 0 and p <= self.entry_price - self.stop_loss
                recover = p >= vwap - self.exit_threshold
                if tp_hit or sl_hit or recover:
                    self._exit(p, i)
            self._mark(p)


class DeepValueStrategy(_LongOnlyBase):
    def __init__(self, entry_price_max=0.25, **kwargs):
        super().__init__(**kwargs)
        self.entry_price_max = entry_price_max
        self._entered = False

    def run(self, prices: List[float]) -> None:
        for i, p in enumerate(prices):
            if not self.in_position and not self._entered:
                if p <= self.entry_price_max:
                    self._enter(p, i)
                    self._entered = True
            self._mark(p)
        # Force-close at last price (resolution)
        if self.in_position and prices:
            self._exit(prices[-1], len(prices) - 1)


class PennyHarvestStrategy(_LongOnlyBase):
    """
    Implements the @paonx_eth dead-contract asymmetric-EV strategy.

    Rules (exact match to the 8 wallet patterns):
      - Enter at any price ≤ entry_max (default 3c)
      - Take profit mechanically at take_profit (default 99c)
      - NO stop loss — the entire loss is already capped at entry price
      - Hold concurrently across many positions (each position in the backtest
        represents one market slot; multi-market is simulated in run_full_backtest)
      - Never re-enter the same market after exit

    EV formula verified from 400M trade dataset:
      EV = (0.0266 × 0.99) + (0.0333 × 0.50) + (0.94 × -entry_price) = +$0.0336 at 1c
    """

    def __init__(self, entry_max=0.03, take_profit=0.99, **kwargs):
        super().__init__(**kwargs)
        self.entry_max   = entry_max
        self.take_profit = take_profit
        self._entered    = False

    def run(self, prices: List[float]) -> None:
        for i, p in enumerate(prices):
            if not self.in_position and not self._entered:
                if p <= self.entry_max:
                    self._enter(p, i)
                    self._entered = True
            elif self.in_position:
                # Take profit at 99c — NO stop loss
                if p >= self.take_profit:
                    self._exit(p, i)
                    self._mark(p)
                    continue
            self._mark(p)
        # If still holding at end, force-close at final price (resolution)
        if self.in_position and prices:
            self._exit(prices[-1], len(prices) - 1)


# ── Strategy registry ─────────────────────────────────────────────────────────

STRATEGY_REGISTRY: Dict[str, Tuple[type, Dict]] = {
    "mean_reversion": (MeanReversionStrategy, {
        "window": 20, "entry_threshold": 0.015,
        "take_profit": 0.05, "stop_loss": 0.03,
    }),
    "breakout": (BreakoutStrategy, {
        "window": 30, "breakout_std": 1.25,
        "max_entry_price": 0.92, "take_profit": 0.02, "stop_loss": 0.02,
        "min_holding": 0, "cooldown": 0,
    }),
    "panic_fade": (PanicFadeStrategy, {
        "drop_window": 80, "min_drop": 0.06, "panic_price": 0.30,
        "rebound_exit": 0.42, "max_holding": 500,
        "take_profit": 0.04, "stop_loss": 0.03,
    }),
    "threshold_momentum": (ThresholdMomentumStrategy, {
        "entry_price": 0.80, "take_profit_price": 0.92, "stop_loss_price": 0.50,
    }),
    "ema_crossover": (EMACrossoverStrategy, {
        "fast_period": 64, "slow_period": 256,
        "entry_buffer": 0.0005, "take_profit": 0.01, "stop_loss": 0.01,
    }),
    "vwap_reversion": (VWAPReversionStrategy, {
        "vwap_window": 80, "entry_threshold": 0.008,
        "exit_threshold": 0.002, "take_profit": 0.015, "stop_loss": 0.02,
    }),
    "deep_value": (DeepValueStrategy, {
        "entry_price_max": 0.25,
    }),
    # Penny harvest: the 1-cent dead-contract asymmetric EV strategy
    # (from @paonx_eth — 400M trades, 6 years, 8 wallets × 100x returns)
    "penny_harvest": (PennyHarvestStrategy, {
        "entry_max": 0.03,     # enter at ≤ 3 cents
        "take_profit": 0.99,   # exit at 99 cents — never earlier
    }),
}


# ── Stats calculator ──────────────────────────────────────────────────────────

def _compute_stats(
    strategy_name: str,
    market_slug: str,
    token_id: str,
    strategy: _LongOnlyBase,
    initial_cash: float,
) -> BacktestResult:
    trades = strategy.trades
    equity = strategy.equity_curve

    n = len(trades)
    wins   = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]

    win_rate    = len(wins) / n if n > 0 else 0.0
    total_pnl   = sum(t.net_pnl for t in trades)
    total_return = total_pnl / initial_cash * 100.0

    gross_wins   = sum(t.net_pnl for t in wins) if wins else 0.0
    gross_losses = abs(sum(t.net_pnl for t in losses)) if losses else 0.0
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else (float("inf") if gross_wins > 0 else 0.0)

    ev = total_pnl / n if n > 0 else 0.0
    avg_dur = sum(t.duration_bars for t in trades) / n if n > 0 else 0.0

    # Sharpe from equity curve returns
    if len(equity) > 2:
        rets = [(equity[i] - equity[i - 1]) / equity[i - 1]
                for i in range(1, len(equity)) if equity[i - 1] > 0]
        if rets:
            mu  = sum(rets) / len(rets)
            var = sum((r - mu) ** 2 for r in rets) / len(rets)
            std = math.sqrt(var) if var > 0 else 1e-9
            sharpe = mu / std * math.sqrt(252)  # annualised
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # Max drawdown from equity curve
    peak = initial_cash
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = (peak - e) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return BacktestResult(
        strategy      = strategy_name,
        market_slug   = market_slug,
        token_id      = token_id,
        bars_replayed = len(equity),
        trades        = n,
        win_rate      = round(win_rate, 4),
        total_return  = round(total_return, 4),
        sharpe        = round(sharpe, 4),
        max_drawdown  = round(max_dd, 4),
        avg_duration  = round(avg_dur, 1),
        profit_factor = round(min(profit_factor, 99.0), 4),
        expected_value= round(ev, 4),
        config        = STRATEGY_REGISTRY[strategy_name][1],
    )


# ── Single market backtest ────────────────────────────────────────────────────

async def backtest_market(
    market_slug: str,
    token_id: str,
    strategy_name: str = "mean_reversion",
    initial_cash: float = 100.0,
    interval: str = "1h",
    fidelity: int = 60,
) -> Optional[BacktestResult]:
    """
    Run one strategy against one market's price history.

    interval/fidelity match the Polymarket prices-history API:
      interval="1d" fidelity=1440  → daily bars
      interval="1h" fidelity=60    → hourly bars
    """
    if strategy_name not in STRATEGY_REGISTRY:
        return None

    try:
        history = await get_price_history(token_id, interval=interval, fidelity=fidelity)
    except Exception as e:
        logger.debug("Price history fetch failed for %s: %s", market_slug, e)
        return None

    if not history or len(history) < 30:
        return None

    prices = [float(h["p"]) for h in history if "p" in h]
    prices = [max(MIN_PRICE, min(MAX_PRICE, p)) for p in prices]

    if len(prices) < 30:
        return None

    StratClass, default_cfg = STRATEGY_REGISTRY[strategy_name]
    strat = StratClass(initial_cash=initial_cash, trade_size=initial_cash * 0.10, **default_cfg)

    try:
        strat.run(prices)
    except Exception as e:
        logger.debug("Strategy %s failed on %s: %s", strategy_name, market_slug, e)
        return None

    return _compute_stats(strategy_name, market_slug, token_id, strat, initial_cash)


# ── Multi-market / multi-strategy runner ──────────────────────────────────────

async def run_full_backtest(
    markets: Optional[List[Dict]] = None,
    strategy_names: Optional[List[str]] = None,
    initial_cash: float = 100.0,
    max_markets: int = 10,
) -> List[BacktestResult]:
    """
    Backtest all requested strategies across multiple markets.

    If markets=None, fetches top 20 markets by volume from Polymarket.
    If strategy_names=None, runs all 7 registered strategies.
    """
    if strategy_names is None:
        strategy_names = list(STRATEGY_REGISTRY.keys())

    if markets is None:
        try:
            markets = await get_top_markets(limit=30)
            markets = [m for m in markets if m.get("yes_token_id")][:max_markets]
        except Exception as e:
            logger.error("Failed to fetch markets for backtest: %s", e)
            return []

    tasks = []
    for market in markets[:max_markets]:
        token_id   = market.get("yes_token_id", "")
        market_slug = market.get("slug", market.get("condition_id", "unknown"))
        if not token_id:
            continue
        for strat_name in strategy_names:
            tasks.append(backtest_market(market_slug, token_id, strat_name,
                                         initial_cash=initial_cash))

    results_raw = await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in results_raw if isinstance(r, BacktestResult)]

    logger.info("Backtest complete: %d results from %d tasks", len(results), len(tasks))
    return results


def aggregate_results(results: List[BacktestResult]) -> Dict:
    """
    Produce a strategy-level summary across all markets.

    Returns:
        {
          "by_strategy": { strategy_name: { avg_return, avg_sharpe, avg_win_rate,
                                            avg_ev, markets_tested, positive_ev_pct } },
          "top_markets": [ {market_slug, best_strategy, total_return} × 5 ],
          "best_strategy": str,
          "timestamp": int,
        }
    """
    if not results:
        return {"by_strategy": {}, "top_markets": [], "best_strategy": None, "timestamp": int(time.time())}

    by_strat: Dict[str, List[BacktestResult]] = {}
    for r in results:
        by_strat.setdefault(r.strategy, []).append(r)

    summary: Dict[str, Dict] = {}
    for strat, rows in by_strat.items():
        n = len(rows)
        summary[strat] = {
            "markets_tested":    n,
            "avg_return_pct":    round(sum(r.total_return for r in rows) / n, 2),
            "avg_sharpe":        round(sum(r.sharpe for r in rows) / n, 3),
            "avg_win_rate":      round(sum(r.win_rate for r in rows) / n, 3),
            "avg_ev":            round(sum(r.expected_value for r in rows) / n, 4),
            "avg_max_drawdown":  round(sum(r.max_drawdown for r in rows) / n, 3),
            "avg_profit_factor": round(sum(r.profit_factor for r in rows) / n, 3),
            "positive_ev_pct":   round(sum(1 for r in rows if r.expected_value > 0) / n, 2),
        }

    # Best strategy by average Sharpe
    best = max(summary.items(), key=lambda x: x[1]["avg_sharpe"])[0]

    # Top 5 individual market/strategy combos by total return
    top_markets = sorted(results, key=lambda r: r.total_return, reverse=True)[:5]
    top_market_list = [
        {
            "market_slug":  r.market_slug,
            "strategy":     r.strategy,
            "total_return": r.total_return,
            "sharpe":       r.sharpe,
            "trades":       r.trades,
        }
        for r in top_markets
    ]

    return {
        "by_strategy":   summary,
        "top_markets":   top_market_list,
        "best_strategy": best,
        "total_results": len(results),
        "timestamp":     int(time.time()),
    }


# ── Continuous background runner ──────────────────────────────────────────────

class BacktestRunner:
    """
    Runs full backtests on a background loop every `interval_s` seconds.
    Results are stored in memory and exposed via get_latest().
    The engine can read best_strategy from get_summary() to bias signal selection.
    """

    def __init__(self, interval_s: int = 1800):  # default: every 30 min
        self.interval_s      = interval_s
        self._running        = False
        self._task: Optional[asyncio.Task] = None
        self._results: List[BacktestResult] = []
        self._summary: Dict  = {}
        self._last_run: float = 0.0
        self._run_count: int  = 0

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("BacktestRunner started (interval=%ds)", self.interval_s)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self):
        # First run immediately so results are available on startup
        await asyncio.sleep(5)   # give the server time to fully start
        while self._running:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("BacktestRunner error: %s", e, exc_info=True)
            await asyncio.sleep(self.interval_s)

    async def _run_once(self):
        logger.info("BacktestRunner: starting backtest sweep…")
        t0 = time.time()
        results = await run_full_backtest(max_markets=8)
        self._results  = results
        self._summary  = aggregate_results(results)
        self._last_run = time.time()
        self._run_count += 1
        elapsed = time.time() - t0
        logger.info(
            "BacktestRunner: sweep #%d done in %.1fs — %d results, best=%s",
            self._run_count, elapsed, len(results),
            self._summary.get("best_strategy", "—"),
        )

    def get_results(self) -> List[Dict]:
        return [
            {
                "strategy":      r.strategy,
                "market_slug":   r.market_slug,
                "bars_replayed": r.bars_replayed,
                "trades":        r.trades,
                "win_rate":      r.win_rate,
                "total_return":  r.total_return,
                "sharpe":        r.sharpe,
                "max_drawdown":  r.max_drawdown,
                "avg_duration":  r.avg_duration,
                "profit_factor": r.profit_factor,
                "expected_value":r.expected_value,
                "timestamp":     int(r.timestamp),
            }
            for r in self._results
        ]

    def get_summary(self) -> Dict:
        return self._summary

    def get_status(self) -> Dict:
        return {
            "running":       self._running,
            "last_run":      int(self._last_run) if self._last_run else None,
            "run_count":     self._run_count,
            "result_count":  len(self._results),
            "next_run_in_s": max(0, int(self._interval_remaining())),
            "strategies":    list(STRATEGY_REGISTRY.keys()),
        }

    def _interval_remaining(self) -> float:
        if not self._last_run:
            return 0.0
        return max(0.0, self.interval_s - (time.time() - self._last_run))


# ── Singleton ──────────────────────────────────────────────────────────────────

_runner_instance: Optional[BacktestRunner] = None


def get_backtest_runner() -> Optional[BacktestRunner]:
    return _runner_instance


def create_backtest_runner(interval_s: int = 1800) -> BacktestRunner:
    global _runner_instance
    _runner_instance = BacktestRunner(interval_s=interval_s)
    return _runner_instance
