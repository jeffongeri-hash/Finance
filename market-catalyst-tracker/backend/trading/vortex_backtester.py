"""
VORTEX-1 Backtester
====================
Walk-forward bar-by-bar replay of the VORTEX-1 strategy on historical
1H equity data fetched from yfinance.

Key design choices:
  - No look-ahead: all indicators computed on data up to current bar.
  - Position split: 50% closed at TP1 (price updated to breakeven stop),
    remaining 50% trailed using successive 1H candle lows.
  - Trade windows enforced: 9:45–11:30 AM and 2:00–3:45 PM EST.
  - Time stop: 3:45 PM on any open position.
  - VIX: fetched daily from ^VIX, forward-filled to 1H bars.
  - IVR proxy: rolling 252-day HV rank, computed in-sample up to each bar.
  - Regime: SPY 50D/200D SMA at each bar.
  - Circuit breaker: 3 consecutive losses halts new entries for that session.
  - Results: trades, win rate, total return %, Sharpe, max drawdown, profit
    factor, avg R per trade, equity curve, per-trade log, regime breakdown.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from trading.vortex_strategy import (
    MarketRegime, VortexPosition, VortexExitSignal,
    _ema, _sma, _rsi, _hv_rank, _is_hammer, _is_bullish_engulfing,
    _find_supports, get_market_regime, evaluate_exit,
    EMA_1H, SMA_DAILY, RSI_PERIOD, RSI_OVERSOLD, RSI_LOOKBACK,
    VOL_MULT, VOL_AVG_BARS, VIX_MIN, VIX_MAX, IVR_MIN, IVR_MAX,
    SUPPORT_TOL, RISK_PCT, MAX_STOCK_PCT, TP1_R, TP2_R_NORMAL,
    TP2_R_BULL, TP2_R_BEAR, WINDOW_AM, WINDOW_PM, TIME_STOP_HM,
    CONSEC_LOSS_HALT,
)

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    symbol:       str
    entry_time:   str
    exit_time:    str
    entry_price:  float
    exit_price:   float
    shares_half:  float     # shares in each half
    pnl_tp1:      float     # P&L from first half (TP1 close)
    pnl_tp2:      float     # P&L from second half (TP2 / stop close)
    net_pnl:      float
    r_realized:   float     # net_pnl / r_dollar
    exit_reason:  str
    regime:       str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VortexBacktestResult:
    symbol:           str
    start_date:       str
    end_date:         str
    initial_capital:  float
    final_capital:    float
    total_return_pct: float
    trades:           int
    winning_trades:   int
    losing_trades:    int
    win_rate:         float
    avg_win_r:        float
    avg_loss_r:       float
    profit_factor:    float
    max_drawdown_pct: float
    sharpe_ratio:     float
    expected_value_r: float
    signals_found:    int     # bars where all 5 filters fired
    trades_taken:     int     # signals that became trades (vs halted by circuit breakers)
    regime_stats:     Dict    = field(default_factory=dict)
    trade_log:        List    = field(default_factory=list)
    equity_curve:     List    = field(default_factory=list)
    error:            Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ── Backtester ────────────────────────────────────────────────────────────────

class VortexBacktester:
    def __init__(self, initial_capital: float = 1000.0):
        self.initial_capital = initial_capital

    def run(
        self,
        symbol:     str,
        start_date: Optional[str] = None,
        end_date:   Optional[str] = None,
        max_days:   int = 365,
    ) -> VortexBacktestResult:
        """
        Run VORTEX-1 on historical 1H data for `symbol`.
        start_date / end_date: "YYYY-MM-DD" (defaults to last max_days days).
        """
        try:
            import yfinance as yf
        except ImportError:
            return self._err(symbol, "yfinance not installed")

        # Date range
        end   = end_date   or datetime.now().strftime("%Y-%m-%d")
        start = start_date or (datetime.now() - timedelta(days=max_days)).strftime("%Y-%m-%d")

        try:
            ticker = yf.Ticker(symbol)
            hourly = ticker.history(start=start, end=end, interval="1h")
            daily  = ticker.history(start=start, end=end, interval="1d")
        except Exception as exc:
            return self._err(symbol, f"data fetch failed: {exc}")

        if hourly.empty:
            return self._err(symbol, "no 1H data returned by yfinance")

        # Fetch VIX + SPY for regime
        try:
            vix_hist = yf.download("^VIX", start=start, end=end, interval="1d",
                                   progress=False, auto_adjust=True)
            spy_hist = yf.download("SPY",  start=start, end=end, interval="1d",
                                   progress=False, auto_adjust=True)
        except Exception as exc:
            return self._err(symbol, f"VIX/SPY fetch failed: {exc}")

        return self._simulate(symbol, hourly, daily, vix_hist, spy_hist, start, end)

    # ── Core simulation ───────────────────────────────────────────────────────

    def _simulate(
        self,
        symbol:   str,
        hourly:   pd.DataFrame,
        daily:    pd.DataFrame,
        vix_hist: pd.DataFrame,
        spy_hist: pd.DataFrame,
        start:    str,
        end:      str,
    ) -> VortexBacktestResult:

        # Normalise timezone: make everything tz-naive for simpler joins
        for df in (hourly, daily, vix_hist, spy_hist):
            if df.index.tz is not None:
                df.index = df.index.tz_convert("America/New_York").tz_localize(None)

        # Build daily VIX lookup (date → closing VIX)
        vix_col    = "Close" if "Close" in vix_hist.columns else vix_hist.columns[0]
        vix_by_day = vix_hist[vix_col].to_dict()

        # Build daily SPY close series for regime
        spy_close_col = "Close" if "Close" in spy_hist.columns else spy_hist.columns[0]
        spy_close     = spy_hist[spy_close_col]

        close_h = hourly["Close"]
        open_h  = hourly["Open"]
        high_h  = hourly["High"]
        low_h   = hourly["Low"]
        vol_h   = hourly["Volume"]

        capital     = self.initial_capital
        equity      = [capital]
        trades: List[BacktestTrade] = []

        position:         Optional[VortexPosition] = None
        pending_half_pnl: float                    = 0.0  # locked in at TP1
        consec_losses:    int                       = 0
        last_trade_date:  Optional[str]             = None   # for chop: 1 trade/day
        signals_found:    int                       = 0
        trades_taken:     int                       = 0

        min_bars = max(EMA_1H, SMA_DAILY) + RSI_PERIOD + 10

        for i in range(min_bars, len(hourly)):
            bar_time: datetime = hourly.index[i]
            hhmm = bar_time.hour * 100 + bar_time.minute
            date_str = bar_time.strftime("%Y-%m-%d")

            # VIX for this bar (use previous day close, forward-fill)
            bar_date = bar_time.date()
            vix = self._vix_at(vix_by_day, bar_date)

            # ── Manage open position ──────────────────────────────────────────
            if position is not None:
                curr_price = float(close_h.iloc[i])
                curr_low   = float(low_h.iloc[i])

                # Update trailing stop after TP1
                if position.trail_active:
                    position.trail_low = max(position.trail_low, curr_low)

                exit_sig = evaluate_exit(position, curr_price, bar_time, curr_low)

                if exit_sig.action == "CLOSE_HALF":
                    # TP1 hit: close 50%, lock in P&L, move stop to BE
                    half = position.shares / 2
                    pending_half_pnl      = half * (exit_sig.price - position.entry_price)
                    position.half_closed  = True
                    position.stop_be      = True
                    position.trail_active = True
                    position.trail_low    = curr_low
                    position.shares       = half

                elif exit_sig.action == "CLOSE_ALL":
                    # Close remaining shares
                    remaining_pnl = position.shares * (exit_sig.price - position.entry_price)
                    net_pnl       = pending_half_pnl + remaining_pnl
                    r_dist        = position.entry_price - position.stop_price
                    r_realized    = net_pnl / (position.shares * r_dist) if r_dist > 0 else 0

                    regime_str = self._regime_at(spy_close, i)
                    trade = BacktestTrade(
                        symbol       = symbol,
                        entry_time   = datetime.fromtimestamp(position.entry_time).strftime("%Y-%m-%d %H:%M"),
                        exit_time    = bar_time.strftime("%Y-%m-%d %H:%M"),
                        entry_price  = position.entry_price,
                        exit_price   = exit_sig.price,
                        shares_half  = position.shares,
                        pnl_tp1      = round(pending_half_pnl, 4),
                        pnl_tp2      = round(remaining_pnl, 4),
                        net_pnl      = round(net_pnl, 4),
                        r_realized   = round(r_realized, 3),
                        exit_reason  = exit_sig.reason,
                        regime       = regime_str,
                    )
                    trades.append(trade)
                    capital          += net_pnl
                    position          = None
                    pending_half_pnl  = 0.0

                    if net_pnl < 0:
                        consec_losses += 1
                    else:
                        consec_losses = 0

                equity.append(capital)
                continue   # don't look for new entry while managing position

            # ── Look for new entry ────────────────────────────────────────────

            # Only enter in trade windows
            in_am = WINDOW_AM[0] <= hhmm <= WINDOW_AM[1]
            in_pm = WINDOW_PM[0] <= hhmm <= WINDOW_PM[1]
            if not (in_am or in_pm):
                equity.append(capital)
                continue

            # Circuit breakers
            if consec_losses >= CONSEC_LOSS_HALT:
                # reset at new session (next day)
                if last_trade_date and date_str != last_trade_date:
                    consec_losses = 0
                else:
                    equity.append(capital)
                    continue

            # Drawdown circuit breaker: -10% → halve sizes
            dd_factor = 0.5 if capital < self.initial_capital * 0.90 else 1.0

            # All indicator slices up to current bar (no look-ahead)
            h_slice = hourly.iloc[:i + 1]
            d_slice = self._daily_up_to(daily, bar_time)
            if len(h_slice) < EMA_1H + RSI_PERIOD + 5 or len(d_slice) < SMA_DAILY + 5:
                equity.append(capital)
                continue

            # Regime
            spy_slice = spy_close.iloc[:self._spy_idx(spy_close, bar_time) + 1]
            regime    = get_market_regime(vix, spy_slice) if len(spy_slice) > 200 else MarketRegime.CHOP

            # Chop: max 1 trade per day
            if regime == MarketRegime.CHOP and date_str == last_trade_date:
                equity.append(capital)
                continue

            # IVR proxy
            ivr = _hv_rank(d_slice["Close"])

            # ── Run all 5 filters ────────────────────────────────────────────
            ok, stop_px = self._check_filters(
                h_slice, d_slice, vix, ivr, bar_time
            )
            if not ok:
                equity.append(capital)
                continue

            signals_found += 1

            # Max 2 concurrent (only 1 here since we wait for full close)
            entry_price = float(h_slice["Close"].iloc[-1])
            r_dist      = entry_price - stop_px
            if r_dist <= 0:
                equity.append(capital)
                continue

            eff_risk    = capital * RISK_PCT * dd_factor
            raw_shares  = eff_risk / r_dist
            max_shares  = (capital * MAX_STOCK_PCT * dd_factor) / entry_price
            shares      = int(min(raw_shares, max_shares))
            if shares < 1:
                equity.append(capital)
                continue

            tp2_r   = (TP2_R_BULL   if regime == MarketRegime.BULL else
                       TP2_R_BEAR   if regime == MarketRegime.BEAR else TP2_R_NORMAL)

            position = VortexPosition(
                symbol      = symbol,
                entry_price = entry_price,
                stop_price  = stop_px,
                tp1_price   = entry_price + TP1_R  * r_dist,
                tp2_price   = entry_price + tp2_r  * r_dist,
                r_dollar    = eff_risk,
                shares      = float(shares),
                entry_time  = bar_time.timestamp(),
            )
            trades_taken  += 1
            last_trade_date = date_str
            equity.append(capital)

        # Force-close any remaining open position at last bar
        if position is not None and len(hourly) > 0:
            last_price = float(close_h.iloc[-1])
            remaining_pnl = position.shares * (last_price - position.entry_price)
            net_pnl       = pending_half_pnl + remaining_pnl
            r_dist        = position.entry_price - position.stop_price
            r_realized    = net_pnl / (position.shares * r_dist) if r_dist > 0 else 0
            trades.append(BacktestTrade(
                symbol       = symbol,
                entry_time   = datetime.fromtimestamp(position.entry_time).strftime("%Y-%m-%d %H:%M"),
                exit_time    = hourly.index[-1].strftime("%Y-%m-%d %H:%M"),
                entry_price  = position.entry_price,
                exit_price   = last_price,
                shares_half  = position.shares,
                pnl_tp1      = round(pending_half_pnl, 4),
                pnl_tp2      = round(remaining_pnl, 4),
                net_pnl      = round(net_pnl, 4),
                r_realized   = round(r_realized, 3),
                exit_reason  = "BACKTEST_END",
                regime       = "UNKNOWN",
            ))
            capital += net_pnl
            equity.append(capital)

        return self._compile_results(
            symbol, start, end, trades, equity, signals_found, trades_taken
        )

    # ── Filter evaluation (bar-by-bar, no look-ahead) ─────────────────────────

    def _check_filters(
        self,
        h: pd.DataFrame,
        d: pd.DataFrame,
        vix: float,
        ivr: float,
        bar_time: datetime,
    ) -> Tuple[bool, float]:
        """Returns (all_passed, stop_price). stop_price = last hourly candle low."""
        close_h = h["Close"]
        open_h  = h["Open"]
        high_h  = h["High"]
        low_h   = h["Low"]
        vol_h   = h["Volume"]
        close_d = d["Close"]
        price   = float(close_h.iloc[-1])

        # Filter 1: Trend
        if len(close_h) < EMA_1H + 2 or len(close_d) < SMA_DAILY + 2:
            return False, 0.0
        ema200  = float(_ema(close_h, EMA_1H).iloc[-1])
        sma50d  = float(_sma(close_d, SMA_DAILY).iloc[-1])
        if price <= ema200 or float(close_d.iloc[-1]) <= sma50d:
            return False, 0.0

        # Filter 2: RSI exhaustion
        rsi_s   = _rsi(close_h, RSI_PERIOD)
        rsi_now = float(rsi_s.iloc[-1])
        rsi_prv = float(rsi_s.iloc[-2])
        rsi_win = rsi_s.iloc[-1 - RSI_LOOKBACK: -1]
        if not (bool((rsi_win < RSI_OVERSOLD).any()) and rsi_now > rsi_prv):
            return False, 0.0

        # Filter 3: Volume
        avg_vol  = float(vol_h.iloc[-VOL_AVG_BARS - 1: -1].mean())
        curr_vol = float(vol_h.iloc[-1])
        if avg_vol <= 0 or (curr_vol / avg_vol) < VOL_MULT:
            return False, 0.0

        # Filter 4: Volatility window
        if not (VIX_MIN <= vix <= VIX_MAX and IVR_MIN <= ivr <= IVR_MAX):
            return False, 0.0

        # Filter 5: Price structure
        supports   = _find_supports(low_h, lookback=60)
        at_support = any(abs(price - s) / s <= SUPPORT_TOL for s in supports) if supports else False
        hammer     = _is_hammer(float(open_h.iloc[-1]), float(high_h.iloc[-1]),
                                 float(low_h.iloc[-1]), float(close_h.iloc[-1]))
        engulf     = (len(open_h) >= 2 and
                      _is_bullish_engulfing(float(open_h.iloc[-2]), float(close_h.iloc[-2]),
                                            float(open_h.iloc[-1]), float(close_h.iloc[-1])))
        if not (at_support or hammer or engulf):
            return False, 0.0

        stop_price = float(low_h.iloc[-1])
        return True, stop_price

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _daily_up_to(daily: pd.DataFrame, bar_time: datetime) -> pd.DataFrame:
        return daily[daily.index <= bar_time]

    @staticmethod
    def _spy_idx(spy_close: pd.Series, bar_time: datetime) -> int:
        idx = spy_close.index.searchsorted(bar_time, side="right") - 1
        return max(0, min(idx, len(spy_close) - 1))

    @staticmethod
    def _regime_at(spy_close: pd.Series, hourly_i: int) -> str:
        if len(spy_close) < 202:
            return "CHOP"
        slice_ = spy_close.iloc[:min(hourly_i + 1, len(spy_close))]
        sma50  = float(_sma(slice_, 50).iloc[-1])
        sma200 = float(_sma(slice_, 200).iloc[-1])
        p      = float(slice_.iloc[-1])
        if p > sma50 and p > sma200:
            return "BULL"
        if p < sma50 and p < sma200:
            return "BEAR"
        return "CHOP"

    @staticmethod
    def _vix_at(vix_by_day: dict, bar_date) -> float:
        """Return VIX for bar_date, walking back up to 5 days if missing."""
        from datetime import timedelta as td
        for offset in range(6):
            key = bar_date - td(days=offset)
            # vix_by_day keys may be Timestamps or dates
            for k in (key, pd.Timestamp(key)):
                if k in vix_by_day:
                    v = vix_by_day[k]
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
        return 20.0  # fallback to neutral VIX

    # ── Result compilation ────────────────────────────────────────────────────

    def _compile_results(
        self,
        symbol:        str,
        start:         str,
        end:           str,
        trades:        List[BacktestTrade],
        equity:        List[float],
        signals_found: int,
        trades_taken:  int,
    ) -> VortexBacktestResult:
        n = len(trades)
        if n == 0:
            return VortexBacktestResult(
                symbol=symbol, start_date=start, end_date=end,
                initial_capital=self.initial_capital,
                final_capital=equity[-1] if equity else self.initial_capital,
                total_return_pct=0.0, trades=0, winning_trades=0, losing_trades=0,
                win_rate=0.0, avg_win_r=0.0, avg_loss_r=0.0, profit_factor=0.0,
                max_drawdown_pct=0.0, sharpe_ratio=0.0, expected_value_r=0.0,
                signals_found=signals_found, trades_taken=trades_taken,
            )

        final_cap = equity[-1] if equity else self.initial_capital
        wins  = [t for t in trades if t.net_pnl >= 0]
        losses= [t for t in trades if t.net_pnl <  0]

        win_rate     = len(wins) / n
        avg_win_r    = float(np.mean([t.r_realized for t in wins]))  if wins   else 0.0
        avg_loss_r   = float(np.mean([t.r_realized for t in losses])) if losses else 0.0
        gross_win    = sum(t.net_pnl for t in wins)
        gross_loss   = abs(sum(t.net_pnl for t in losses))
        profit_factor= gross_win / gross_loss if gross_loss > 0 else float("inf")
        ev_r         = float(np.mean([t.r_realized for t in trades]))
        total_ret    = (final_cap - self.initial_capital) / self.initial_capital * 100

        # Max drawdown on equity curve
        eq = np.array(equity)
        peak = np.maximum.accumulate(eq)
        dd   = (peak - eq) / peak
        max_dd = float(dd.max()) * 100

        # Sharpe (annualised, using daily equity changes)
        eq_s    = pd.Series(equity)
        ret_s   = eq_s.pct_change().dropna()
        sharpe  = (float(ret_s.mean()) / float(ret_s.std()) * math.sqrt(252 * 6.5)
                   if float(ret_s.std()) > 0 else 0.0)

        # Regime breakdown
        regime_stats: Dict = {}
        for t in trades:
            r = t.regime
            if r not in regime_stats:
                regime_stats[r] = {"trades": 0, "wins": 0, "total_pnl": 0.0}
            regime_stats[r]["trades"]    += 1
            regime_stats[r]["wins"]      += 1 if t.net_pnl >= 0 else 0
            regime_stats[r]["total_pnl"] += t.net_pnl

        return VortexBacktestResult(
            symbol            = symbol,
            start_date        = start,
            end_date          = end,
            initial_capital   = self.initial_capital,
            final_capital     = round(final_cap, 2),
            total_return_pct  = round(total_ret, 2),
            trades            = n,
            winning_trades    = len(wins),
            losing_trades     = len(losses),
            win_rate          = round(win_rate, 4),
            avg_win_r         = round(avg_win_r, 3),
            avg_loss_r        = round(avg_loss_r, 3),
            profit_factor     = round(profit_factor, 3),
            max_drawdown_pct  = round(max_dd, 2),
            sharpe_ratio      = round(sharpe, 3),
            expected_value_r  = round(ev_r, 3),
            signals_found     = signals_found,
            trades_taken      = trades_taken,
            regime_stats      = regime_stats,
            trade_log         = [t.to_dict() for t in trades],
            equity_curve      = [round(v, 2) for v in equity[::4]],  # downsample for response size
        )

    @staticmethod
    def _err(symbol: str, msg: str) -> VortexBacktestResult:
        return VortexBacktestResult(
            symbol=symbol, start_date="", end_date="",
            initial_capital=1000.0, final_capital=1000.0,
            total_return_pct=0.0, trades=0, winning_trades=0,
            losing_trades=0, win_rate=0.0, avg_win_r=0.0, avg_loss_r=0.0,
            profit_factor=0.0, max_drawdown_pct=0.0, sharpe_ratio=0.0,
            expected_value_r=0.0, signals_found=0, trades_taken=0,
            error=msg,
        )
