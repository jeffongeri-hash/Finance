"""
Trade Proposal Agent — documents opportunities for HUMAN approval. It cannot
execute anything; approval and paper submission happen in the orchestrator
behind an authenticated API call.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from qqq.macro_calendar import MacroCalendar
from qqq.models import (CreditSpread, ExitPlan, PaperPosition, RiskDecision, TradeProposal,
                        UnderlyingSnapshot)
from qqq.rules import ExitRules, FeeModel
from qqq.screening import iv_preference_note

AGENT = "trade_proposal_agent"
PROPOSAL_TTL = timedelta(minutes=15)


class ProposalAgent:
    name = AGENT

    def __init__(self, fees: FeeModel, exit_rules: ExitRules, calendar: MacroCalendar):
        self.fees = fees
        self.exit_rules = exit_rules
        self.calendar = calendar

    def entry(self, spread: CreditSpread, snap: UnderlyingSnapshot, risk: RiskDecision, env: str,
              strategy_version: str, validation: str, now: datetime) -> TradeProposal:
        s = spread
        summ = s.summary(self.fees, now.date())
        side = "above" if s.strategy == "bull_put" else "below"
        thesis = [
            f"Trend: {snap.trend.value} — {snap.trend_reason}.",
            f"Sell the {s.short.strike:g}{s.right} / buy the {s.long.strike:g}{s.right} "
            f"({s.short.expiry:%b %d}, {summ['dte']} DTE) for a ${s.limit_credit:.2f} limit credit; "
            f"profits if QQQ stays {side} {summ['breakeven']:.2f} at expiration.",
            f"Short leg |Δ| {abs(s.short.delta or 0):.3f}; net θ {summ['net_theta'] or 0:+.3f} $/day; "
            f"net Γ {summ['net_gamma'] or 0:+.4f}; net vega {summ['net_vega'] or 0:+.3f} "
            f"(Greeks: {s.short.greeks_source}).",
            iv_preference_note(snap.iv_context) or "",
        ]
        if s.strategy == "bull_put":
            invalidation = [
                f"QQQ closes below the 50-day SMA ({snap.sma50:.2f}) or 200-day SMA ({snap.sma200:.2f}) — trend thesis void",
                f"QQQ trades through the short strike {s.short.strike:g}",
                f"Short-put |Δ| reaches {self.exit_rules.delta_warn:.2f}–{self.exit_rules.delta_urgent:.2f}",
            ]
        else:
            invalidation = [
                f"QQQ closes above the 50-day SMA ({snap.sma50:.2f}) or 200-day SMA ({snap.sma200:.2f}) — trend thesis void",
                f"QQQ trades through the short strike {s.short.strike:g}",
                f"Short-call |Δ| reaches {self.exit_rules.delta_warn:.2f}–{self.exit_rules.delta_urgent:.2f}",
            ]
        deadline, why = (None, "")
        if not self.calendar.coverage_gaps(s.short.expiry):
            deadline, why = self.calendar.position_deadline(now, s.short.expiry, self.exit_rules)
        plan = ExitPlan(
            profit_target_debit=round(s.limit_credit * (1 - self.exit_rules.profit_target_pct), 2),
            delta_warn=self.exit_rules.delta_warn, delta_urgent=self.exit_rules.delta_urgent,
            event_exit_deadline=deadline, event_exit_reason=why,
        )
        return TradeProposal(
            id=f"prop-{uuid.uuid4().hex[:10]}", kind="entry", created_at=now, expires_at=now + PROPOSAL_TTL,
            env=env, strategy_version=strategy_version, strategy_validation=validation,
            spread=summ, spread_model=s, thesis=[t for t in thesis if t], invalidation=invalidation,
            exit_plan=plan, risk=risk, warnings=list(risk.warnings),
        )

    def exit(self, pos: PaperPosition, reasons: List[str], debit_natural: Optional[float],
             risk: RiskDecision, env: str, now: datetime) -> TradeProposal:
        return TradeProposal(
            id=f"exit-{uuid.uuid4().hex[:10]}", kind="exit", created_at=now, expires_at=now + PROPOSAL_TTL,
            env=env, strategy_version="n/a", strategy_validation="n/a",
            spread={"position_id": pos.id, "strategy": pos.strategy, "short_strike": pos.short_strike,
                    "long_strike": pos.long_strike, "expiry": pos.expiry.isoformat(),
                    "entry_credit": pos.entry_credit, "contracts": pos.contracts,
                    "limit_debit": debit_natural},
            thesis=[f"Exit flagged: {r}" for r in reasons],
            invalidation=["Exit proposals are advisory; a human decides whether to close."],
            exit_plan=pos.exit_plan, risk=risk, position_id=pos.id, exit_reasons=reasons,
            warnings=["Closing order uses a limit at the natural debit; fills are not guaranteed."],
        )
