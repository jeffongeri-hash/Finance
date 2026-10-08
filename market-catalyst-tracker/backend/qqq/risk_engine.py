"""
Risk Management Engine — deterministic, no overrides.

* Every rule is plain Python over immutable inputs. No LLM, network call or
  agent output participates in a decision.
* There is no `force`, `override` or `skip` parameter anywhere in this module.
* Any exception raised while evaluating → the trade is REJECTED (fail closed).
* A trade is approved only if EVERY check passes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from qqq.macro_calendar import MacroCalendar
from qqq.models import (CreditSpread, DataQualityReport, PaperPosition, RiskCheck,
                        RiskDecision, Trend)
from qqq.rules import UNDERLYING, ExitRules, FeeModel, RiskLimits

logger = logging.getLogger(__name__)

TREND_FOR_STRATEGY = {"bull_put": Trend.BULLISH, "bear_call": Trend.BEARISH}


@dataclass(frozen=True)
class RiskContext:
    now: datetime
    env: str
    trend: Trend
    data_quality: DataQualityReport
    kill_switch_engaged: bool
    reconciliation_ok: bool
    open_positions: int
    working_orders: int
    realized_pnl_today: float
    available_cash: float
    calendar: MacroCalendar
    strategy_validated: bool


class RiskEngine:
    def __init__(self, limits: RiskLimits, fees: FeeModel, exit_rules: ExitRules,
                 allow_sandbox_limits: bool = False):
        if limits.sandbox and not allow_sandbox_limits:
            raise ValueError("sandbox (what-if) limits cannot be used outside research backtests")
        self._limits = limits
        self._fees = fees
        self._exit = exit_rules

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    # ── entry ─────────────────────────────────────────────────────────────────

    def evaluate_entry(self, spread: CreditSpread, ctx: RiskContext) -> RiskDecision:
        try:
            checks, warnings = self._entry_checks(spread, ctx)
        except Exception as exc:     # fail closed
            logger.exception("risk engine error")
            checks, warnings = [RiskCheck(name="risk_engine_error", passed=False, detail=repr(exc))], []
        return RiskDecision(approved=bool(checks) and all(c.passed for c in checks),
                            checks=checks, warnings=warnings,
                            limits_version=self._limits.version(), evaluated_at=ctx.now)

    def _entry_checks(self, s: CreditSpread, ctx: RiskContext):
        L, F = self._limits, self._fees
        checks: List[RiskCheck] = []
        warnings: List[str] = []

        def add(name: str, ok: bool, detail: str = "") -> None:
            checks.append(RiskCheck(name=name, passed=bool(ok), detail=detail))

        # Global controls
        if L.sandbox:
            warnings.append("WHAT-IF sandbox limits in use — research only, not the user's rules")
        add("kill_switch_off", not ctx.kill_switch_engaged,
            "kill switch engaged" if ctx.kill_switch_engaged else "")
        add("environment_allows_paper_only", ctx.env in ("research", "paper"),
            f"env={ctx.env}; live execution is not implemented")
        add("data_quality_ok", ctx.data_quality.ok, "; ".join(ctx.data_quality.issues[:5]))
        add("broker_reconciled", ctx.reconciliation_ok,
            "" if ctx.reconciliation_ok else "positions/orders not reconciled with broker ledger")
        add("daily_loss_limit", ctx.realized_pnl_today > -L.daily_loss_limit_usd,
            f"today's realized P&L {ctx.realized_pnl_today:.2f} vs limit −{L.daily_loss_limit_usd:.2f}")
        add("max_open_positions", ctx.open_positions + ctx.working_orders < L.max_open_positions,
            f"{ctx.open_positions} open, {ctx.working_orders} working; max {L.max_open_positions}")

        # Instrument / structure (defined risk only)
        legs = (s.short, s.long)
        add("underlying_is_qqq", all(q.underlying.upper() == UNDERLYING for q in legs),
            f"{s.short.underlying}/{s.long.underlying}")
        add("allowed_strategy", s.strategy in L.allowed_strategies, s.strategy)
        same = s.short.expiry == s.long.expiry and s.short.right == s.long.right
        add("vertical_same_expiry_and_type", same,
            f"{s.short.expiry}{s.short.right} vs {s.long.expiry}{s.long.right}")
        right_ok = (s.strategy == "bull_put" and s.right == "P") or (s.strategy == "bear_call" and s.right == "C")
        add("strategy_matches_option_type", right_ok, f"{s.strategy} uses {s.right}")
        protective = (s.long.strike < s.short.strike) if s.right == "P" else (s.long.strike > s.short.strike)
        add("defined_risk_long_wing", protective and s.width > 0,
            f"short {s.short.strike} long {s.long.strike}")
        add("equal_leg_quantity_not_naked", s.contracts >= 1, f"{s.contracts} × 1:1 vertical")
        # Cross-leg consistency (no-arbitrage ordering). Legs quoted at different moments —
        # e.g. separate delayed snapshots — can violate this; such data is not tradable.
        if s.short.delta is not None and s.long.delta is not None:
            consistent = abs(s.short.delta) > abs(s.long.delta) and s.short.mid > s.long.mid \
                and s.credit_mid <= s.width
            add("legs_quote_consistent", consistent,
                f"|Δ| {abs(s.short.delta):.3f} vs {abs(s.long.delta):.3f}; mid {s.short.mid} vs {s.long.mid}")

        # Trend rule
        need = TREND_FOR_STRATEGY.get(s.strategy)
        add("trend_not_ambiguous", ctx.trend != Trend.AMBIGUOUS, f"trend={ctx.trend.value}")
        add("trend_matches_strategy", need is not None and ctx.trend == need,
            f"{s.strategy} requires {need.value if need else '?'}; trend={ctx.trend.value}")

        # Expiration
        dte = s.dte(ctx.now.date())
        add("not_0dte", dte > 0, f"DTE={dte}")
        add("dte_window", L.min_dte <= dte <= L.max_dte, f"DTE={dte}, allowed {L.min_dte}–{L.max_dte}")

        # Greeks
        add("greeks_present", s.short.has_greeks and s.long.has_greeks,
            f"sources: {s.short.greeks_source}/{s.long.greeks_source}")
        sd = abs(s.short.delta) if s.short.delta is not None else None
        add("short_delta_band", sd is not None and L.short_delta_min <= sd <= L.short_delta_max,
            f"|Δ|={sd:.3f}" if sd is not None else "delta missing")
        theta = s.net_theta()
        add("positive_net_theta", (theta is not None and theta > 0) or not L.require_positive_theta,
            f"net θ={theta:.4f}/day" if theta is not None else "theta missing")

        # Economics
        add("positive_credit", 0 < s.limit_credit < s.width,
            f"limit credit {s.limit_credit:.2f} on width {s.width:.2f}")
        ml = s.max_loss(F)
        add("max_loss_incl_fees", ml <= L.max_loss_usd,
            f"max loss ${ml:.2f} (incl. ${s.fees(F):.2f} est. round-trip fees) vs cap ${L.max_loss_usd:.2f}")
        add("cash_covers_max_loss", ctx.available_cash >= ml,
            f"available ${ctx.available_cash:.2f} vs max loss ${ml:.2f}")

        # Gamma relative to premium — threshold only from validated research
        gtc = s.gamma_to_credit()
        if L.max_gamma_to_credit is None:
            warnings.append("gamma-to-credit filter UNVALIDATED: no researched threshold is configured "
                            f"(this spread: {gtc})")
        else:
            add("gamma_to_credit", gtc is not None and gtc <= L.max_gamma_to_credit,
                f"{gtc} vs max {L.max_gamma_to_credit}")

        # Macro events
        gaps = ctx.calendar.coverage_gaps(s.short.expiry)
        add("macro_calendar_verified", not gaps, "; ".join(gaps))
        if not gaps:
            deadline, why = ctx.calendar.position_deadline(ctx.now, s.short.expiry, self._exit)
            if deadline is not None:
                hours = (deadline - ctx.now).total_seconds() / 3600
                add("time_before_event_exit", hours >= L.min_hours_to_exit_deadline,
                    f"must exit by {deadline.isoformat()} for {why}; {hours:.1f}h away "
                    f"(min {L.min_hours_to_exit_deadline}h)")

        # Strategy evidence
        if not ctx.strategy_validated:
            warnings.append("strategy has NO independently validated backtest — paper trading only")
        return checks, warnings

    # ── exits (proposals only) ────────────────────────────────────────────────

    def exit_signals(self, pos: PaperPosition, debit_to_close: Optional[float],
                     short_delta: Optional[float], ctx_now: datetime,
                     calendar: MacroCalendar, short_itm: Optional[bool] = None) -> List[str]:
        reasons: List[str] = []
        E = self._exit
        try:
            if debit_to_close is not None and debit_to_close <= pos.entry_credit * (1 - E.profit_target_pct):
                reasons.append(f"profit_target: debit {debit_to_close:.2f} ≤ "
                               f"{pos.entry_credit * (1 - E.profit_target_pct):.2f} (50% of max profit)")
            if short_delta is not None:
                ad = abs(short_delta)
                if ad >= E.delta_urgent:
                    reasons.append(f"delta_urgent: |Δshort|={ad:.2f} ≥ {E.delta_urgent}")
                elif ad >= E.delta_warn:
                    reasons.append(f"delta_warning: |Δshort|={ad:.2f} ≥ {E.delta_warn}")
            else:
                reasons.append("data_missing: short-leg delta unavailable — review position manually")
            gaps = calendar.coverage_gaps(pos.expiry)
            if gaps:
                reasons.append("macro_calendar_unverified: " + "; ".join(gaps))
            else:
                deadline, why = calendar.position_deadline(ctx_now, pos.expiry, E)
                if deadline is not None and ctx_now >= deadline - timedelta(hours=1):
                    reasons.append(f"event_deadline: exit by {deadline.isoformat()} before {why}")
            dte = (pos.expiry - ctx_now.date()).days
            if dte <= E.expiration_warning_dte:
                reasons.append(f"expiration_risk: {dte} DTE — pin and assignment risk")
            if short_itm:
                reasons.append("assignment_risk: short leg is in the money (American-style; early "
                               "assignment possible, especially before ex-dividend)")
        except Exception as exc:
            reasons.append(f"exit_evaluation_error: {exc!r} — review position manually")
        return reasons

