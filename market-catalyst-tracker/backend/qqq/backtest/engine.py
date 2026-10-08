"""
Backtesting Agent — event-driven, end-of-day, point-in-time.

Timing model (documented in every report):
  * Day d's decision uses bars ≤ d and day d's end-of-day chain only.
  * Entries signalled on d are filled on the chain of d + `fill_lag_days`
    (default 1), and only if that day's fill credit ≥ the signalled limit.
  * Exits are decided and filled on the same EOD snapshot (slippage applied).
    Pre-event exits happen on the last snapshot before the deadline.
  * Unexercised positions are settled at intrinsic value on expiration
    (QQQ is physically settled — the report counts assignment-risk days).

Fill model: each leg fills at mid ∓ s × half-spread, s = `slippage_fraction`
(s = 1 → natural bid/ask; s = 0 → mid, which the validator rejects).
Missing Greeks → no entry that day (counted). Greeks are never back-filled.
The SAME deterministic RiskEngine used for live proposals gates every entry.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from qqq.backtest.data import HistoricalSource, PointInTimeView, eod
from qqq.backtest.metrics import compute_metrics, json_safe
from qqq.indicators import percentile_rank
from qqq.macro_calendar import MacroCalendar
from qqq.market_data import build_snapshot, validate_quote
from qqq.models import (BacktestReport, BacktestTrade, CreditSpread, DataQualityReport,
                        OptionChain, OptionQuote, PaperPosition, Trend)
from qqq.risk_engine import RiskContext, RiskEngine
from qqq.rules import (CONTRACT_MULTIPLIER, STARTING_BALANCE_USD, UNDERLYING, DataQualityRules,
                       ExitRules, FeeModel, RiskLimits)
from qqq.screening import build_candidates


@dataclass
class BacktestConfig:
    start: date
    end: date
    strategy_version: str = "qqq-trend-credit-spread-v1"
    proposer: str = "strategy_research_agent"
    slippage_fraction: float = 0.5
    fill_lag_days: int = 1
    max_width: float = 5.0
    delta_target: Optional[float] = None          # prefer short |Δ| closest to this (inside the band)
    delta_exit: float = 0.30                      # within the user's 0.30–0.35 flag band
    starting_balance: float = STARTING_BALANCE_USD
    require_macro_calendar: bool = True

    def params(self) -> Dict[str, Any]:
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        return d


def leg_fill(q: OptionQuote, buy: bool, s: float) -> float:
    half = (q.ask - q.bid) / 2
    return q.mid + s * half if buy else q.mid - s * half


def _index(quotes: List[OptionQuote]) -> Dict[Tuple[date, float, str], OptionQuote]:
    return {(q.expiry, q.strike, q.right): q for q in quotes}


class Backtester:
    name = "backtesting_agent"

    def __init__(self, source: HistoricalSource, limits: RiskLimits, fees: FeeModel,
                 exit_rules: ExitRules, calendar: Optional[MacroCalendar] = None):
        self.source = source
        self.view = PointInTimeView(source)
        self.limits = limits
        self.fees = fees
        self.exit_rules = exit_rules
        self.calendar = calendar or MacroCalendar([], {})
        self.risk = RiskEngine(limits, fees, exit_rules, allow_sandbox_limits=True)
        self.dq = DataQualityRules(max_quote_age_seconds=10**9)

    def run(self, cfg: BacktestConfig) -> BacktestReport:
        if cfg.delta_target is not None and not (self.limits.short_delta_min <= cfg.delta_target
                                                 <= self.limits.short_delta_max):
            raise ValueError("delta_target must lie inside the risk-engine delta band")
        if not (self.exit_rules.delta_warn <= cfg.delta_exit <= self.exit_rules.delta_urgent):
            raise ValueError("delta_exit must lie inside the 0.30–0.35 flag band")
        if not (0.0 <= cfg.slippage_fraction <= 1.0):
            raise ValueError("slippage_fraction must be in [0, 1]")

        cal = self.calendar
        if cfg.require_macro_calendar:
            gaps = self.calendar.history_gaps(cfg.start) + self.calendar.coverage_gaps(cfg.end)
            if gaps:
                raise ValueError("macro calendar does not cover the backtest window "
                                 "(historical CPI/FOMC/NFP dates are required): " + "; ".join(gaps))
        else:
            # Explicitly disabled: coverage treated as verified, but the report is flagged and
            # the validator will not approve it.
            cal = MacroCalendar(self.calendar.events, {k: date.max for k in _all_kinds()},
                                self.calendar.holidays, self.calendar.early_closes)

        days = self.source.trading_days(cfg.start, cfg.end)
        cash = cfg.starting_balance
        pos: Optional[PaperPosition] = None
        pos_meta: Dict[str, Any] = {}
        pending: Optional[Tuple[date, CreditSpread, Optional[float]]] = None
        trades: List[BacktestTrade] = []
        equity: List[List[Any]] = []
        missing_data = missing_greeks = assignment_risk_days = 0
        atm_iv_hist: List[float] = []      # point-in-time: only days already simulated
        self._last_mark: Optional[float] = None
        self._stale_marks = 0

        for i, d in enumerate(days):
            self.view.cursor = d
            now = eod(d)
            bars = self.view.bars()
            quotes = self.view.chain(d)
            if not quotes or len(bars) < 200:
                missing_data += 1
                equity.append([d.isoformat(), round(cash + self._mtm(pos, None, cfg), 2)])
                continue
            idx = _index(quotes)
            spot = bars[-1].close
            atm_iv = _atm_iv(quotes, spot, d)
            ivp = percentile_rank(atm_iv_hist[-252:], atm_iv) if atm_iv is not None else None
            if atm_iv is not None:
                atm_iv_hist.append(atm_iv)

            # 1) fill pending entry
            if pending and pending[0] <= d:
                _, sig, sig_ivp = pending
                pending = None
                sq, lq = idx.get((sig.short.expiry, sig.short.strike, sig.right)), \
                    idx.get((sig.long.expiry, sig.long.strike, sig.right))
                if sq and lq:
                    credit = leg_fill(sq, False, cfg.slippage_fraction) - leg_fill(lq, True, cfg.slippage_fraction)
                    if credit >= sig.limit_credit - 1e-9 and sig.limit_credit > 0:
                        fees_open = round(2 * sig.contracts * self.fees.per_contract(), 2)
                        cash += sig.limit_credit * CONTRACT_MULTIPLIER * sig.contracts - fees_open
                        pos = PaperPosition(
                            id=uuid.uuid4().hex[:8], strategy=sig.strategy, short_symbol=sig.short.osi(),
                            long_symbol=sig.long.osi(), expiry=sig.short.expiry, short_strike=sig.short.strike,
                            long_strike=sig.long.strike, right=sig.right, contracts=sig.contracts,
                            entry_credit=sig.limit_credit, entry_fees=fees_open, opened_at=now)
                        self._last_mark = sig.limit_credit
                        pos_meta = {"entry_day": d, "max_loss": sig.max_loss(self.fees),
                                    "gtc": sig.gamma_to_credit(), "delta": sig.short.delta,
                                    "ivp": sig_ivp}

            # 2) manage open position
            if pos is not None:
                sq = idx.get((pos.expiry, pos.short_strike, pos.right))
                lq = idx.get((pos.expiry, pos.long_strike, pos.right))
                if d >= pos.expiry:
                    debit = _intrinsic(pos, spot)
                    fees_close = 0.0 if debit == 0 else round(2 * pos.contracts * self.fees.assignment_fee, 2)
                    cash, pos = self._close(trades, pos, pos_meta, d, debit, fees_close, "expiration", cash)
                elif sq and lq:
                    debit = max(leg_fill(sq, True, cfg.slippage_fraction)
                                - leg_fill(lq, False, cfg.slippage_fraction), 0.0)
                    debit = min(debit, pos.width)
                    itm = (spot < pos.short_strike) if pos.right == "P" else (spot > pos.short_strike)
                    if itm and (pos.expiry - d).days <= self.exit_rules.expiration_warning_dte:
                        assignment_risk_days += 1
                    reason = self._exit_reason(pos, debit, sq.delta, now, days, i, cal, cfg)
                    if reason:
                        fees_close = round(2 * pos.contracts * self.fees.per_contract(), 2)
                        cash, pos = self._close(trades, pos, pos_meta, d, round(debit, 4), fees_close, reason, cash)

            # 3) look for a new entry (one position at a time)
            if pos is None and pending is None and i + cfg.fill_lag_days < len(days):
                snap = build_snapshot(bars, spot, now, source=self.source.name)
                if snap.trend != Trend.AMBIGUOUS:
                    clean = [q for q in quotes if not _hard_issues(q, now, self.dq)]
                    if clean:
                        chain = OptionChain(underlying=UNDERLYING, asof=now, spot=spot, quotes=clean)
                        cands = build_candidates(chain, snap.trend, self.limits, self.fees, now,
                                                 max_width=cfg.max_width, top_n=200)
                        side = "P" if snap.trend == Trend.BULLISH else "C"
                        if not cands and any(q.delta is None for q in quotes if q.right == side):
                            missing_greeks += 1
                        if cfg.delta_target is not None:
                            cands.sort(key=lambda c: (c.max_loss(self.fees),
                                                      abs(abs(c.short.delta) - cfg.delta_target)))
                        ctx = RiskContext(now=now, env="research", trend=snap.trend,
                                          data_quality=DataQualityReport(ok=True), kill_switch_engaged=False,
                                          reconciliation_ok=True, open_positions=0, working_orders=0,
                                          realized_pnl_today=0.0, available_cash=cash, calendar=cal,
                                          strategy_validated=True)
                        for c in cands:
                            if self.risk.evaluate_entry(c, ctx).approved:
                                pending = (days[i + cfg.fill_lag_days], c, ivp)
                                break
                    else:
                        missing_greeks += 1

            equity.append([d.isoformat(), round(cash + self._mtm(pos, idx, cfg), 2)])

        pnls = [t.pnl for t in trades]
        metrics = json_safe(compute_metrics(pnls, [e[1] for e in equity], cfg.starting_balance))
        return BacktestReport(
            id=f"bt-{uuid.uuid4().hex[:10]}", strategy_version=cfg.strategy_version, proposer=cfg.proposer,
            data_source=self.source.name, synthetic=bool(getattr(self.source, "synthetic", True)),
            lookahead_guard=True, fill_model="mid_plus_slippage", slippage_fraction=cfg.slippage_fraction,
            fill_lag_days=cfg.fill_lag_days, macro_filter=cfg.require_macro_calendar,
            start=cfg.start, end=cfg.end, params={**cfg.params(), "assignment_risk_days": assignment_risk_days,
                    "sandbox_limits": self.limits.sandbox,
                    "stale_mark_days": self._stale_marks, "limits_version": self.limits.version()},
            trades=trades, equity_curve=equity, days_total=len(days), days_missing_data=missing_data,
            days_missing_greeks=missing_greeks, metrics=metrics,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _exit_reason(self, pos, debit, short_delta, now, days, i, cal, cfg) -> Optional[str]:
        if debit <= pos.entry_credit * (1 - self.exit_rules.profit_target_pct):
            return "profit_target"
        if short_delta is not None and abs(short_delta) >= cfg.delta_exit:
            return "delta_flag"
        if cal.verified_through:
            deadline, _ = cal.position_deadline(now, pos.expiry, self.exit_rules)
            nxt = eod(days[i + 1]) if i + 1 < len(days) else None
            if deadline is not None and (nxt is None or deadline < nxt):
                return "pre_event_exit"
        return None

    def _close(self, trades, pos, meta, d, debit, fees_close, reason, cash):
        cash -= debit * CONTRACT_MULTIPLIER * pos.contracts + fees_close
        pnl = (pos.entry_credit - debit) * CONTRACT_MULTIPLIER * pos.contracts - pos.entry_fees - fees_close
        trades.append(BacktestTrade(
            strategy=pos.strategy, entry_day=meta["entry_day"], exit_day=d, expiry=pos.expiry,
            short_strike=pos.short_strike, long_strike=pos.long_strike, contracts=pos.contracts,
            entry_credit=pos.entry_credit, exit_debit=debit, fees=round(pos.entry_fees + fees_close, 2),
            pnl=round(pnl, 2), exit_reason=reason, max_loss_at_entry=meta["max_loss"],
            gamma_to_credit=meta.get("gtc"), short_delta=meta.get("delta"), iv_percentile=meta.get("ivp")))
        return cash, None

    def _mtm(self, pos, idx, cfg) -> float:
        if pos is None:
            return 0.0
        if idx:
            sq = idx.get((pos.expiry, pos.short_strike, pos.right))
            lq = idx.get((pos.expiry, pos.long_strike, pos.right))
            if sq and lq:
                self._last_mark = min(max(sq.mid - lq.mid, 0.0), pos.width)
                return -self._last_mark * CONTRACT_MULTIPLIER * pos.contracts
        # Leg missing from that day's chain: carry the last observed mark forward (counted).
        self._stale_marks += 1
        mark = self._last_mark if self._last_mark is not None else pos.entry_credit
        return -mark * CONTRACT_MULTIPLIER * pos.contracts


def _intrinsic(pos: PaperPosition, s: float) -> float:
    if pos.right == "P":
        v = max(0.0, pos.short_strike - s) - max(0.0, pos.long_strike - s)
    else:
        v = max(0.0, s - pos.short_strike) - max(0.0, s - pos.long_strike)
    return round(min(max(v, 0.0), pos.width), 4)


def _atm_iv(quotes: List[OptionQuote], spot: float, d: date) -> Optional[float]:
    """ATM IV of the expiry nearest 30 DTE, from that day's quotes only."""
    with_iv = [q for q in quotes if q.iv and q.iv > 0 and (q.expiry - d).days >= 7]
    if not with_iv:
        return None
    exp = min({q.expiry for q in with_iv}, key=lambda e: abs((e - d).days - 30))
    near = sorted((q for q in with_iv if q.expiry == exp), key=lambda q: abs(q.strike - spot))[:2]
    return sum(q.iv for q in near) / len(near) if near else None


def _hard_issues(q: OptionQuote, now: datetime, rules: DataQualityRules) -> List[str]:
    return validate_quote(q, now, rules)


def _all_kinds():
    from qqq.models import MacroKind
    return list(MacroKind)
