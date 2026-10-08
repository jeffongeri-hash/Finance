"""
Independent Validation Agent.

Reviews a BacktestReport (and its walk-forward results) produced by someone
else. It recomputes every metric from raw trades rather than trusting the
report, and it refuses to validate work it proposed itself.

Evidence policy (conservative defaults; these gate APPROVAL of evidence, they
are not trading limits and cannot loosen any risk rule):
  * real historical option data only (no synthetic chains)
  * look-ahead guard on, fill model worse than mid, macro filter on
  * walk-forward present with ≥ MIN_OOS_TRADES out-of-sample trades
  * positive OOS expectancy and OOS profit factor > 1 after fees
  * missing-data days ≤ 20% of the sample
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from qqq.backtest.metrics import compute_metrics, json_safe
from qqq.models import BacktestReport, ValidationReport

MIN_OOS_TRADES = 30
MAX_MISSING_DATA_FRACTION = 0.20
MIN_SLIPPAGE_FRACTION = 0.25
MAX_DEGRADATION = 0.5          # OOS expectancy must be ≥ 50% of IS expectancy
MAX_DRAWDOWN_PCT = 0.40        # of starting balance
VALIDATOR_ID = "independent_validation_agent"


class SelfValidationError(PermissionError):
    pass


class ValidationAgent:
    name = VALIDATOR_ID

    def validate(self, rep: BacktestReport, requested_by: Optional[str] = None) -> ValidationReport:
        if rep.proposer == VALIDATOR_ID or (requested_by and requested_by == rep.proposer
                                            and requested_by != "human"):
            raise SelfValidationError("the agent that proposed a strategy may not validate its own results")
        reasons: List[str] = []
        warnings: List[str] = []

        if rep.params.get("sandbox_limits"):
            reasons.append("WHAT-IF sandbox limits looser than the user's rules — research only")
        if rep.synthetic:
            reasons.append("backtest used SYNTHETIC option prices — not evidence of edge")
        if not rep.lookahead_guard:
            reasons.append("look-ahead guard not active")
        if rep.slippage_fraction < MIN_SLIPPAGE_FRACTION:
            reasons.append(f"fill model too optimistic (slippage {rep.slippage_fraction} < "
                           f"{MIN_SLIPPAGE_FRACTION} of half-spread)")
        if rep.fill_lag_days < 1:
            warnings.append("entries fill on the signal snapshot (fill_lag_days=0)")
        if not rep.macro_filter:
            reasons.append("macro-event exit filter disabled — results ignore CPI/FOMC/NFP rule")
        if rep.days_total and rep.days_missing_data / rep.days_total > MAX_MISSING_DATA_FRACTION:
            reasons.append(f"{rep.days_missing_data}/{rep.days_total} days missing data "
                           f"(> {MAX_MISSING_DATA_FRACTION:.0%})")
        if rep.days_missing_greeks:
            warnings.append(f"{rep.days_missing_greeks} days skipped for missing Greeks (not back-filled)")

        # Independent recomputation (in-sample, whole period)
        sb = float(rep.params.get("starting_balance", 250.0))
        full = json_safe(compute_metrics([t.pnl for t in rep.trades], [e[1] for e in rep.equity_curve], sb))
        for k in ("trades", "net_pnl", "expectancy"):
            if rep.metrics.get(k) != full.get(k):
                reasons.append(f"reported {k}={rep.metrics.get(k)} does not match recomputed {full.get(k)}")

        # Unrealistic fills: credits above the mid-to-mid value cannot be checked without
        # quotes, but credits ≥ width or negative max loss are impossible.
        for t in rep.trades:
            width = abs(t.short_strike - t.long_strike)
            if t.entry_credit >= width or t.entry_credit <= 0:
                reasons.append(f"impossible fill on {t.entry_day}: credit {t.entry_credit} vs width {width}")
                break
            if t.pnl < -(t.max_loss_at_entry + 0.01):
                reasons.append(f"trade on {t.entry_day} lost more than its max loss — accounting error")
                break

        wf = rep.walk_forward
        oos: Dict[str, Any] = {}
        if not wf:
            reasons.append("no walk-forward / out-of-sample results")
        else:
            oos_pnls = [t["pnl"] for t in wf.get("oos_trades", [])]
            eq = [sb]
            for p in oos_pnls:
                eq.append(eq[-1] + p)
            oos = json_safe(compute_metrics(oos_pnls, eq, sb))
            if oos["trades"] < MIN_OOS_TRADES:
                reasons.append(f"only {oos['trades']} out-of-sample trades (< {MIN_OOS_TRADES})")
            if oos["expectancy"] is None or oos["expectancy"] <= 0:
                reasons.append(f"out-of-sample expectancy {oos['expectancy']} ≤ 0 after fees")
            pf = oos.get("profit_factor")
            if pf is None or (pf != "inf" and pf <= 1.0):
                reasons.append(f"out-of-sample profit factor {pf} ≤ 1")
            if (oos.get("max_drawdown") or 0) > MAX_DRAWDOWN_PCT * sb:
                reasons.append(f"out-of-sample max drawdown ${oos['max_drawdown']} exceeds "
                               f"{MAX_DRAWDOWN_PCT:.0%} of starting balance")
            is_exp = (wf.get("is_metrics") or {}).get("expectancy")
            if is_exp and is_exp > 0 and oos.get("expectancy") is not None \
                    and oos["expectancy"] < MAX_DEGRADATION * is_exp:
                reasons.append(f"OOS expectancy {oos['expectancy']} < {MAX_DEGRADATION:.0%} of IS {is_exp} "
                               "— likely overfit")
            grid = wf.get("grid_size", 1)
            if grid > max(1, oos["trades"] // 10):
                warnings.append(f"{grid} parameter sets tried for {oos['trades']} OOS trades — "
                                "multiple-testing risk")

        return ValidationReport(
            id=f"val-{uuid.uuid4().hex[:10]}", backtest_id=rep.id, strategy_version=rep.strategy_version,
            validator=VALIDATOR_ID, proposer=rep.proposer,
            status="REJECTED" if reasons else "APPROVED", reasons=reasons, warnings=warnings,
            metrics={"full_period": full, "out_of_sample": oos},
        )
