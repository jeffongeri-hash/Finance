"""
Orchestrator — wires the agents into the workflow and owns safe start/stop.

  Market Data → Signal Screening → Strategy Research (validation status)
  → Risk Engine → Trade Proposal → [HUMAN APPROVAL] → Paper Broker

Backtesting → Independent Validation run separately (`run_backtest`) and feed
the "strategy validated" flag used by proposals.

Fail-closed rules implemented here:
  * Any exception in a cycle → outcome NO_TRADE, alert raised, nothing submitted.
  * Startup reconciles the paper ledger; a mismatch engages the kill switch.
  * Approval RE-RUNS the risk engine with fresh data and state; stale or
    expired proposals are rejected.
  * Daily-loss breach or reconciliation failure engages the kill switch.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from qqq.backtest.data import (AlphaVantageHistoricalSource, CsvHistoricalSource, HistoricalSource,
                               SyntheticSource)
from qqq.backtest.engine import Backtester, BacktestConfig
from qqq.backtest.walk_forward import walk_forward
from qqq.kill_switch import KillSwitch
from qqq.macro_calendar import DEFAULT_PATH as MACRO_PATH, ET, MacroCalendar
from qqq.market_data import (DataQualityError, DataUnavailable, MarketDataAgent, YFinanceProvider,
                             AlphaVantageProvider, validate_quote)
from qqq.models import (BacktestReport, CreditSpread, DataQualityReport, OptionChain, OptionQuote,
                        RiskCheck, RiskDecision, TradeProposal, Trend, ValidationReport, utcnow)
from qqq.notifications import Notifier
from qqq.paper_broker import OrderRejected, PaperBroker
from qqq.proposals import ProposalAgent
from qqq.research import ResearchAgent
from qqq.review import ReviewAgent
from qqq.risk_engine import RiskContext, RiskEngine
from qqq.rules import (DataQualityRules, ExitRules, load_env, load_fee_model, load_market_assumptions,
                       load_risk_limits)
from qqq.screening import build_candidates
from qqq.store import Store
from qqq.validation import ValidationAgent

logger = logging.getLogger(__name__)
STRATEGY_VERSION = "qqq-trend-credit-spread-v1"
BASE_DIR = Path(__file__).resolve().parent.parent


class Orchestrator:
    def __init__(self, store: Optional[Store] = None, market: Optional[MarketDataAgent] = None,
                 calendar: Optional[MacroCalendar] = None, env: Optional[str] = None,
                 notifier: Optional[Notifier] = None, clock=None):
        self.clock = clock or utcnow
        self.env = env or load_env()
        self.limits = load_risk_limits()
        self.fees = load_fee_model()
        self.mkt = load_market_assumptions()
        self.exit_rules = ExitRules()
        self.dq_rules = DataQualityRules()
        self.store = store or Store(os.getenv("QQQ_DB_PATH") or BASE_DIR / f"qqq_{self.env}.db")
        self.calendar = calendar or MacroCalendar.load(MACRO_PATH)
        self.kill = KillSwitch(self.store)
        self.broker = PaperBroker(self.store, self.fees)
        self.risk = RiskEngine(self.limits, self.fees, self.exit_rules)
        self.research = ResearchAgent(self.store)
        self.review = ReviewAgent(self.store, self.research)
        self.validator = ValidationAgent()
        self.proposer = ProposalAgent(self.fees, self.exit_rules, self.calendar)
        av = AlphaVantageProvider() if os.getenv("ALPHA_VANTAGE_API_KEY") else None
        self.market = market or MarketDataAgent(YFinanceProvider(self.mkt), self.dq_rules, av)
        self.notify = notifier or Notifier(self.store)
        self._lock = threading.RLock()
        self._task: Optional[asyncio.Task] = None
        self.last_cycle: Optional[Dict[str, Any]] = None

    # ── activity log ──────────────────────────────────────────────────────────

    def activity(self, agent: str, message: str, level: str = "info", data: Optional[dict] = None) -> None:
        import uuid
        aid = f"{self.clock().timestamp():.6f}-{uuid.uuid4().hex[:4]}"
        self.store.insert("activity", aid, {"at": self.clock().isoformat(), "agent": agent, "level": level,
                                            "message": message, "data": data or {}})

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def startup(self) -> Dict[str, Any]:
        ok, issues = self.broker.reconcile()
        if not ok:
            self.kill.engage("startup reconciliation failed: " + "; ".join(issues), "system")
            self.notify.alert("risk", "Kill switch engaged at startup", "; ".join(issues))
        self.store.audit("system", "startup", {"env": self.env, "reconciled": ok, "issues": issues,
                                               "limits_version": self.limits.version()})
        self.activity("orchestrator", f"startup env={self.env} reconciled={ok}")
        return {"reconciled": ok, "issues": issues}

    async def start_scheduler(self, interval_s: int = 300) -> None:
        if self._task:
            return

        async def loop():
            while True:
                try:
                    now = self.clock()
                    if self.calendar.is_market_open(now):
                        await asyncio.get_running_loop().run_in_executor(None, self.run_cycle, "scheduler")
                        await asyncio.get_running_loop().run_in_executor(None, self.monitor, "scheduler")
                except Exception:
                    logger.exception("scheduler iteration failed")
                await asyncio.sleep(interval_s)

        self._task = asyncio.create_task(loop())

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        self.store.audit("system", "shutdown", {"env": self.env})

    # ── state helpers ─────────────────────────────────────────────────────────

    def validated_report(self) -> Optional[ValidationReport]:
        for v in self.store.list("validations", 200):
            if v["status"] == "APPROVED" and v["strategy_version"] == STRATEGY_VERSION:
                return ValidationReport(**v)
        return None

    def _risk_context(self, now: datetime, trend: Trend, dq: DataQualityReport) -> RiskContext:
        rec_ok, _ = self.broker.reconcile()
        today = now.astimezone(ET).date()
        realized = self.broker.realized_pnl_on(today)
        if realized <= -self.limits.daily_loss_limit_usd and not self.kill.engaged():
            self.kill.engage(f"daily loss limit hit ({realized:.2f})", "risk_engine")
            self.notify.alert("risk", "Daily loss limit hit", f"Realized P&L today {realized:.2f}")
        return RiskContext(
            now=now, env=self.env, trend=trend, data_quality=dq, kill_switch_engaged=self.kill.engaged(),
            reconciliation_ok=rec_ok, open_positions=len(self.broker.positions("open")),
            working_orders=len(self.broker.active_orders()), realized_pnl_today=realized,
            available_cash=self.broker.available_cash(), calendar=self.calendar,
            strategy_validated=self.validated_report() is not None)

    # ── main cycle ────────────────────────────────────────────────────────────

    def run_cycle(self, actor: str = "human") -> Dict[str, Any]:
        with self._lock:
            now = self.clock()
            cycle: Dict[str, Any] = {"id": f"cyc-{now.timestamp():.0f}", "at": now.isoformat(), "actor": actor,
                                     "outcome": "NO_TRADE", "candidates": [], "proposals": []}
            try:
                snap, _ = self.market.snapshot(now)
                cycle["snapshot"] = snap.model_dump(mode="json")
                self.activity("market_data_agent", f"QQQ {snap.price:.2f}, trend {snap.trend.value}")
                if snap.trend == Trend.AMBIGUOUS:
                    cycle["outcome_reason"] = "trend_ambiguous"
                    return self._finish(cycle, f"No trade: {snap.trend_reason}")
                if not self.calendar.is_market_open(now):
                    cycle["outcome_reason"] = "market_closed"
                    cycle["note"] = "Chains outside regular hours are stale; proposals are only made in-session."
                chain = self.market.chain(snap.price, self.limits.min_dte, self.limits.max_dte, now)
                dq, clean = self._screen_quotes(chain, now)
                if cycle.get("outcome_reason") == "market_closed":
                    dq.ok = False
                    dq.issues.insert(0, "market closed — quotes not live")
                self.activity("market_data_agent", f"{len(chain.quotes)} quotes, {len(clean.quotes)} passed checks",
                              data={"issues": dq.issues[:10]})
                cands = build_candidates(clean, snap.trend, self.limits, self.fees, now)
                self.activity("signal_screening", f"{len(cands)} candidate spreads")
                ctx = self._risk_context(now, snap.trend, dq)
                validated = self.validated_report()
                approved: List[tuple] = []
                for c in cands:
                    dec = self.risk.evaluate_entry(c, ctx)
                    cycle["candidates"].append({**c.summary(self.fees, now.date()),
                                                "risk": dec.model_dump(mode="json")})
                    if dec.approved:
                        approved.append((c, dec))
                self.activity("risk_engine", f"{len(approved)}/{len(cands)} candidates passed all checks")
                if not cands:
                    cycle["outcome_reason"] = cycle.get("outcome_reason") or "no_candidates_in_delta_dte_band"
                    return self._finish(cycle, "No candidates in the delta/DTE band")
                if not approved:
                    fails = {}
                    for row in cycle["candidates"]:
                        for chk in row["risk"]["checks"]:
                            if not chk["passed"]:
                                fails[chk["name"]] = fails.get(chk["name"], 0) + 1
                    cycle["rejection_summary"] = fails
                    cycle["outcome_reason"] = cycle.get("outcome_reason") or max(fails, key=fails.get)
                    return self._finish(cycle, f"All candidates rejected: {fails}")
                spread, dec = approved[0]
                prop = self.proposer.entry(spread, snap, dec, self.env, STRATEGY_VERSION,
                                           f"validated:{validated.id}" if validated else "UNVALIDATED", now)
                self.store.insert("proposals", prop.id, prop.model_dump(mode="json"))
                self.store.audit("trade_proposal_agent", "proposal.create", {"id": prop.id, **prop.spread})
                cycle["proposals"].append(prop.id)
                cycle["outcome"] = "PROPOSAL"
                self.notify.alert("proposal", "Qualified QQQ trade proposal",
                                  f"{prop.spread['strategy']} {prop.spread['short_strike']:g}/"
                                  f"{prop.spread['long_strike']:g} {prop.spread['expiry']} credit "
                                  f"{prop.spread['limit_credit']:.2f} max loss ${prop.spread['max_loss']:.2f}",
                                  {"proposal_id": prop.id})
                return self._finish(cycle, f"Proposal {prop.id} awaiting human approval")
            except DataQualityError as exc:
                cycle["outcome_reason"] = "data_quality"
                cycle["issues"] = exc.report.issues
                self.notify.alert("risk", "Market data rejected", "; ".join(exc.report.issues[:5]))
                return self._finish(cycle, f"Data rejected: {exc}")
            except Exception as exc:
                logger.exception("cycle failed")
                cycle["outcome_reason"] = "error"
                cycle["error"] = repr(exc)
                self.notify.alert("risk", "Pipeline error — no trade", repr(exc))
                return self._finish(cycle, f"Error: {exc!r}")

    def _finish(self, cycle: Dict[str, Any], msg: str) -> Dict[str, Any]:
        cycle["message"] = msg
        self.store.insert("cycles", cycle["id"], cycle)
        self.activity("orchestrator", msg, data={"cycle": cycle["id"], "outcome": cycle["outcome"]})
        self.last_cycle = cycle
        return cycle

    def _screen_quotes(self, chain: OptionChain, now: datetime):
        issues: List[str] = []
        clean = []
        for q in chain.quotes:
            qi = validate_quote(q, now, self.dq_rules)
            if qi:
                issues += qi
            else:
                clean.append(q)
        dq = DataQualityReport(ok=bool(clean), issues=[] if clean else ["no quotes passed validation"] + issues[:20],
                               warnings=issues[:50])
        return dq, chain.model_copy(update={"quotes": clean})

    # ── approvals ─────────────────────────────────────────────────────────────

    def _get_proposal(self, pid: str) -> TradeProposal:
        d = self.store.get("proposals", pid)
        if not d:
            raise KeyError(pid)
        return TradeProposal(**d)

    def _save_proposal(self, p: TradeProposal) -> None:
        self.store.upsert("proposals", p.id, p.model_dump(mode="json"))

    def _fresh_legs(self, expiry: date, short_strike: float, long_strike: float, right: str, now: datetime):
        spot, _ = self.market.provider.spot("QQQ")
        quotes = self.market.provider.option_chain("QQQ", expiry, spot)
        by = {(q.strike, q.right): q for q in quotes}
        return spot, by.get((short_strike, right)), by.get((long_strike, right))

    def approve(self, pid: str, by: str, note: str = "") -> Dict[str, Any]:
        with self._lock:
            p = self._get_proposal(pid)
            if p.status == "submitted":
                return {"proposal": p.model_dump(mode="json"), "idempotent_replay": True}
            if p.status != "pending_approval":
                raise PermissionError(f"proposal is {p.status}")
            now = self.clock()
            if now > p.expires_at:
                p = p.model_copy(update={"status": "expired", "decided_at": now, "decided_by": "system"})
                self._save_proposal(p)
                raise PermissionError("proposal expired — quotes are stale; run a new cycle")
            if p.kind == "entry":
                return self._approve_entry(p, by, note, now)
            return self._approve_exit(p, by, note, now)

    def _approve_entry(self, p: TradeProposal, by: str, note: str, now: datetime) -> Dict[str, Any]:
        s: CreditSpread = p.spread_model
        snap, _ = self.market.snapshot(now)
        spot, sq, lq = self._fresh_legs(s.short.expiry, s.short.strike, s.long.strike, s.right, now)
        if not sq or not lq:
            raise PermissionError("could not refresh leg quotes — not submitting")
        fresh = s.model_copy(update={"short": sq, "long": lq, "asof": now})
        dq = DataQualityReport(ok=not (validate_quote(sq, now, self.dq_rules) or validate_quote(lq, now, self.dq_rules)),
                               issues=validate_quote(sq, now, self.dq_rules) + validate_quote(lq, now, self.dq_rules))
        if not self.calendar.is_market_open(now):
            dq.ok, dq.issues = False, ["market closed"] + dq.issues
        dec = self.risk.evaluate_entry(fresh, self._risk_context(now, snap.trend, dq))
        if not dec.approved:
            p = p.model_copy(update={"status": "rejected", "decided_by": "risk_engine", "decided_at": now,
                                     "decision_note": "re-check at approval failed: " +
                                     "; ".join(f"{c.name}: {c.detail}" for c in dec.failed())})
            self._save_proposal(p)
            self.store.audit("risk_engine", "proposal.recheck_failed", {"id": p.id})
            return {"proposal": p.model_dump(mode="json"), "risk": dec.model_dump(mode="json")}
        order = self.broker.submit_open(p, s, now)
        order = self.broker.try_fill(order.client_order_id, sq, lq, now)
        p = p.model_copy(update={"status": "submitted", "decided_by": by, "decided_at": now, "decision_note": note})
        self._save_proposal(p)
        self.store.audit(by, "proposal.approve", {"id": p.id, "order": order.client_order_id})
        self.activity("human", f"approved {p.id}; paper order {order.status}")
        return {"proposal": p.model_dump(mode="json"), "order": order.model_dump(mode="json")}

    def _approve_exit(self, p: TradeProposal, by: str, note: str, now: datetime) -> Dict[str, Any]:
        pos = self.broker.position(p.position_id)
        if pos is None or pos.status != "open":
            raise PermissionError("position is not open")
        _, sq, lq = self._fresh_legs(pos.expiry, pos.short_strike, pos.long_strike, pos.right, now)
        if not sq or not lq:
            raise PermissionError("could not refresh leg quotes — not submitting")
        limit = round(min(max(sq.ask - lq.bid, 0.0), pos.width), 2)
        order = self.broker.submit_close(p, pos, limit, now)
        order = self.broker.try_fill(order.client_order_id, sq, lq, now)
        p = p.model_copy(update={"status": "submitted", "decided_by": by, "decided_at": now, "decision_note": note})
        self._save_proposal(p)
        self.store.audit(by, "proposal.approve_exit", {"id": p.id, "order": order.client_order_id})
        return {"proposal": p.model_dump(mode="json"), "order": order.model_dump(mode="json")}

    def reject(self, pid: str, by: str, note: str = "") -> Dict[str, Any]:
        with self._lock:
            p = self._get_proposal(pid)
            if p.status != "pending_approval":
                return {"proposal": p.model_dump(mode="json")}
            p = p.model_copy(update={"status": "rejected", "decided_by": by, "decided_at": utcnow(),
                                     "decision_note": note})
            self._save_proposal(p)
            self.store.audit(by, "proposal.reject", {"id": pid, "note": note})
            return {"proposal": p.model_dump(mode="json")}

    # ── monitoring / exits ────────────────────────────────────────────────────

    def monitor(self, actor: str = "human") -> Dict[str, Any]:
        with self._lock:
            now = self.clock()
            out = {"at": now.isoformat(), "positions": [], "orders": []}
            # retry working orders (DAY orders: anything from a prior session is cancelled)
            for o in self.broker.active_orders():
                if o.created_at.astimezone(ET).date() < now.astimezone(ET).date():
                    o = self.broker.cancel(o.client_order_id, "system:day_order_expired")
                    out["orders"].append(o.model_dump(mode="json"))
                    if o.filled_contracts:
                        self.notify.alert("risk", "Partially filled order expired",
                                          f"{o.client_order_id}: {o.filled_contracts}/{o.contracts} filled")
                    continue
                try:
                    _, sq, lq = self._fresh_legs(o.expiry, o.short_strike, o.long_strike, o.right, now)
                    if sq and lq:
                        o = self.broker.try_fill(o.client_order_id, sq, lq, now)
                    out["orders"].append(o.model_dump(mode="json"))
                except Exception as exc:
                    out["orders"].append({"client_order_id": o.client_order_id, "error": repr(exc)})
            pending_exit = {p["position_id"] for p in self.store.list("proposals", 500)
                            if p.get("kind") == "exit" and p.get("status") == "pending_approval"
                            and datetime.fromisoformat(p["expires_at"]) > now}
            pending_exit |= {o.position_id for o in self.broker.active_orders() if o.intent == "close"}
            for pos in self.broker.positions("open"):
                row: Dict[str, Any] = {"position_id": pos.id}
                try:
                    if now.astimezone(ET).date() > pos.expiry:
                        spot, _ = self.market.provider.spot("QQQ")
                        settled = self.broker.settle_expiration(pos, spot, now)
                        row["settled"] = settled.model_dump(mode="json")
                        self.notify.alert("risk", "Position expired", f"{pos.id} settled at intrinsic")
                        out["positions"].append(row)
                        continue
                    spot, sq, lq = self._fresh_legs(pos.expiry, pos.short_strike, pos.long_strike, pos.right, now)
                    debit_nat = round(min(max(sq.ask - lq.bid, 0.0), pos.width), 2) if sq and lq else None
                    debit_mid = round(sq.mid - lq.mid, 2) if sq and lq else None
                    itm = (spot < pos.short_strike) if pos.right == "P" else (spot > pos.short_strike)
                    reasons = self.risk.exit_signals(pos, debit_mid, sq.delta if sq else None, now,
                                                     self.calendar, itm)
                    row.update({"spot": spot, "debit_natural": debit_nat, "debit_mid": debit_mid,
                                "short_delta": sq.delta if sq else None, "exit_reasons": reasons,
                                "unrealized_pnl": round((pos.entry_credit - (debit_mid or pos.width)) * 100 *
                                                        pos.contracts - pos.entry_fees, 2)})
                    if reasons and pos.id not in pending_exit:
                        dec = RiskDecision(approved=True, checks=[RiskCheck(name="exit_always_allowed", passed=True,
                                                                           detail="risk-reducing")],
                                           limits_version=self.limits.version())
                        prop = self.proposer.exit(pos, reasons, debit_nat, dec, self.env, now)
                        self.store.insert("proposals", prop.id, prop.model_dump(mode="json"))
                        row["exit_proposal"] = prop.id
                        level = "risk" if any(r.startswith(("delta_urgent", "event_deadline", "assignment"))
                                              for r in reasons) else "proposal"
                        self.notify.alert(level, f"Exit flagged for {pos.id}", "; ".join(reasons),
                                          {"proposal_id": prop.id})
                except Exception as exc:
                    row["error"] = repr(exc)
                    self.notify.alert("risk", f"Could not evaluate {pos.id}", repr(exc))
                out["positions"].append(row)
            ok, issues = self.broker.reconcile()
            out["reconciliation"] = {"ok": ok, "issues": issues}
            if not ok and not self.kill.engaged():
                self.kill.engage("reconciliation mismatch: " + "; ".join(issues), "risk_engine")
                self.notify.alert("risk", "Kill switch engaged", "; ".join(issues))
            self.store.kv_set("last_monitor", out)
            return out

    # ── backtesting + validation ──────────────────────────────────────────────

    def build_source(self, kind: str, start: date, csv_dir: Optional[str] = None) -> HistoricalSource:
        bars_provider = YFinanceProvider(self.mkt)
        lookback = (date.today() - start).days + 420
        if kind == "csv":
            if not csv_dir:
                raise ValueError("csv_dir required")
            return CsvHistoricalSource(Path(csv_dir))
        bars = bars_provider.daily_bars("QQQ", lookback)
        if kind == "alphavantage":
            if not os.getenv("ALPHA_VANTAGE_API_KEY"):
                raise DataUnavailable("ALPHA_VANTAGE_API_KEY not set (premium key required for HISTORICAL_OPTIONS)")
            return AlphaVantageHistoricalSource(bars, BASE_DIR / "qqq_cache" / "alphavantage")
        if kind == "synthetic":
            vxn = bars_provider.daily_bars("^VXN", lookback)
            return SyntheticSource(bars, {b.day: b.close / 100 for b in vxn}, self.mkt)
        raise ValueError(f"unknown source {kind}")

    def run_backtest(self, kind: str, start: date, end: date, csv_dir: Optional[str] = None,
                     grid: Optional[List[Dict[str, Any]]] = None, require_macro: bool = True,
                     source: Optional[HistoricalSource] = None) -> Dict[str, Any]:
        src = source or self.build_source(kind, start, csv_dir)
        bt = Backtester(src, self.limits, self.fees, self.exit_rules, self.calendar)
        base = BacktestConfig(start=start, end=end, strategy_version=STRATEGY_VERSION,
                              require_macro_calendar=require_macro)
        self.activity("backtesting_agent", f"backtest {src.name} {start}→{end}")
        rep = bt.run(base)
        grid = grid or [{"delta_target": 0.12}, {"delta_target": 0.14}]
        rep.walk_forward = walk_forward(bt, base, grid)
        self.store.insert("backtests", rep.id, rep.model_dump(mode="json"))
        val = self.validator.validate(rep, requested_by="orchestrator")
        self.store.insert("validations", val.id, val.model_dump(mode="json"))
        self.activity("independent_validation_agent", f"{rep.id}: {val.status}", data={"reasons": val.reasons})
        self.research.evaluate(rep, val.status)
        self.store.audit("independent_validation_agent", "validation", {"backtest": rep.id, "status": val.status})
        return {"backtest": _brief(rep), "validation": val.model_dump(mode="json")}

    # ── dashboard ─────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        now = self.clock()
        ok, issues = self.broker.reconcile()
        nxt = self.calendar.next_event(now)
        positions = self.broker.positions()
        closed = [p for p in positions if p.status == "closed"]
        pnls = [p.realized_pnl or 0 for p in closed]
        start_bal = float(self.store.kv_get("paper_starting_balance", 250.0))
        validated = self.validated_report()
        return {
            "env": self.env, "now": now.isoformat(), "market_open": self.calendar.is_market_open(now),
            "kill_switch": self.kill.state(),
            "reconciliation": {"ok": ok, "issues": issues},
            "limits": {**self.limits.__dict__, "version": self.limits.version()},
            "fees": self.fees.__dict__, "exit_rules": self.exit_rules.__dict__,
            "macro": {"next_event": nxt.model_dump(mode="json") if nxt else None,
                      "next_event_exit_deadline": (self.calendar.exit_deadline_for(nxt, self.exit_rules).isoformat()
                                                   if nxt else None),
                      "coverage_gaps_21d": self.calendar.coverage_gaps(now.date() + timedelta(days=21))},
            "paper": {"starting_balance": start_bal, "cash": self.broker.cash(),
                      "collateral": self.broker.collateral(), "available": self.broker.available_cash(),
                      "realized_pnl": round(sum(pnls), 2), "closed_trades": len(closed),
                      "win_rate": round(sum(1 for x in pnls if x > 0) / len(pnls), 3) if pnls else None,
                      "today_realized": self.broker.realized_pnl_on(now.astimezone(ET).date())},
            "strategy": {"version": STRATEGY_VERSION,
                         "validation": f"validated:{validated.id}" if validated else "UNVALIDATED"},
            "last_cycle": _cycle_brief(self.last_cycle or (self.store.list("cycles", 1) or [None])[0]),
            "audit_chain_ok": self.store.verify_audit(),
        }


def _brief(rep: BacktestReport) -> Dict[str, Any]:
    d = rep.model_dump(mode="json")
    d["trades"] = d["trades"][-50:]
    d["equity_curve"] = d["equity_curve"][::max(1, len(d["equity_curve"]) // 300)]
    if d.get("walk_forward"):
        d["walk_forward"] = {k: v for k, v in d["walk_forward"].items() if k != "oos_trades"}
    return d


def _cycle_brief(c: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not c:
        return None
    return {k: c.get(k) for k in ("id", "at", "actor", "outcome", "outcome_reason", "message",
                                  "rejection_summary", "issues", "snapshot", "note")}
