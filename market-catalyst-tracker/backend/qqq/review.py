"""
Review & Improvement Agent.

Analyses rejected proposals and closed paper/backtest trades, writes to the
versioned research log, and files change proposals. It can never:
  * loosen a risk limit (risk-limit keys are rejected outright unless the change
    strictly tightens them, and even then it must be applied by a human via env),
  * activate a strategy-parameter change without an APPROVED independent
    validation report for that exact strategy version AND a human approver.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from qqq.models import ValidationReport, utcnow
from qqq.research import ResearchAgent
from qqq.store import Store

AGENT = "review_agent"
RISK_LIMIT_KEYS = {"max_loss_usd", "max_open_positions", "min_dte", "max_dte", "short_delta_min",
                   "short_delta_max", "daily_loss_limit_usd", "require_positive_theta",
                   "allowed_strategies", "max_gamma_to_credit", "min_hours_to_exit_deadline"}
STRATEGY_PARAM_KEYS = {"delta_target", "max_width", "delta_exit"}


class ChangeRejected(PermissionError):
    pass


class ReviewAgent:
    name = AGENT

    def __init__(self, store: Store, research: ResearchAgent):
        self.store = store
        self.research = research

    def analyze(self) -> Dict[str, Any]:
        proposals = self.store.list("proposals", 500)
        failed = Counter()
        for p in proposals:
            for c in (p.get("risk") or {}).get("checks", []):
                if not c["passed"]:
                    failed[c["name"]] += 1
        cycles = self.store.list("cycles", 500)
        no_trade = Counter(c.get("outcome_reason", "?") for c in cycles if c.get("outcome") == "NO_TRADE")
        closed = [p for p in self.store.list("positions", 500) if p.get("status") == "closed"]
        pnls = [p.get("realized_pnl") or 0 for p in closed]
        summary = {
            "proposals_reviewed": len(proposals),
            "top_rejection_checks": failed.most_common(8),
            "no_trade_reasons": no_trade.most_common(8),
            "paper_trades_closed": len(closed),
            "paper_net_pnl": round(sum(pnls), 2),
            "paper_win_rate": round(sum(1 for x in pnls if x > 0) / len(pnls), 3) if pnls else None,
            "observations": [],
        }
        if failed.get("max_loss_incl_fees"):
            summary["observations"].append(
                "Most candidates fail the $50 max-loss cap: at 0.10–0.15 delta, a $1-wide QQQ spread "
                "collects far less than the ~$0.53 credit needed. This is a RULE CONFLICT for the "
                "human to resolve; the review agent will not loosen the cap.")
        if no_trade.get("macro_calendar_unverified") or failed.get("macro_calendar_verified"):
            summary["observations"].append("Macro calendar is not verified — fill qqq/data/macro_events.json.")
        self.research.log("review", "Periodic review", summary, author=AGENT)
        return summary

    def propose_change(self, changes: Dict[str, Any], rationale: str, strategy_version: str) -> Dict[str, Any]:
        bad = set(changes) & RISK_LIMIT_KEYS
        if bad:
            raise ChangeRejected(f"risk limits cannot be changed by agents: {sorted(bad)}")
        unknown = set(changes) - STRATEGY_PARAM_KEYS
        if unknown:
            raise ChangeRejected(f"unknown strategy parameters: {sorted(unknown)}")
        cid = f"chg-{self.store.count('change_proposals') + 1:04d}"
        doc = {"id": cid, "kind": "strategy_params", "changes": changes, "rationale": rationale,
               "strategy_version": strategy_version, "status": "requires_independent_backtest",
               "proposed_by": AGENT, "created_at": utcnow().isoformat()}
        self.store.insert("change_proposals", cid, doc)
        self.research.log("change_proposed", f"{cid}: {changes}", doc, author=AGENT)
        return doc

    def activate_change(self, change_id: str, validation: Optional[ValidationReport], approved_by: str) -> Dict:
        doc = self.store.get("change_proposals", change_id)
        if not doc:
            raise ChangeRejected("unknown change")
        if doc.get("kind") != "strategy_params":
            raise ChangeRejected("only strategy-parameter changes can be activated here; risk "
                                 "tightenings are applied by a human through environment variables")
        if not approved_by or approved_by in (AGENT, "strategy_research_agent", "independent_validation_agent"):
            raise ChangeRejected("a human approver is required")
        if validation is None or validation.status != "APPROVED":
            raise ChangeRejected("requires an APPROVED independent validation report")
        if validation.strategy_version != doc["strategy_version"]:
            raise ChangeRejected("validation report is for a different strategy version")
        params = self.store.kv_get("strategy_params", {}) or {}
        params.update(doc["changes"])
        self.store.kv_set("strategy_params", params)
        doc.update({"status": "active", "approved_by": approved_by, "validation_id": validation.id,
                    "activated_at": utcnow().isoformat()})
        self.store.upsert("change_proposals", change_id, doc)
        self.store.audit(approved_by, "change.activate", doc)
        return doc
