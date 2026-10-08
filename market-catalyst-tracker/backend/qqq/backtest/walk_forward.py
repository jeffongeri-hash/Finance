"""
Walk-forward optimisation with out-of-sample evaluation.

For each fold: run every parameter set on the TRAIN window, select the best by
train expectancy, then run ONLY that set on the following, non-overlapping TEST
window. OOS trades from all folds are concatenated for validation. The grid may
only contain parameters inside the user's rules (the engine enforces bands).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Dict, List

from qqq.backtest.engine import Backtester, BacktestConfig
from qqq.backtest.metrics import compute_metrics, json_safe

ALLOWED_GRID_KEYS = {"delta_target", "max_width", "delta_exit"}


def walk_forward(bt: Backtester, base: BacktestConfig, grid: List[Dict[str, Any]],
                 train_days: int = 365, test_days: int = 91, min_train_trades: int = 5) -> Dict[str, Any]:
    for g in grid:
        bad = set(g) - ALLOWED_GRID_KEYS
        if bad:
            raise ValueError(f"grid keys not allowed (risk limits are not tunable): {bad}")
    grid = grid or [{}]
    folds: List[Dict[str, Any]] = []
    oos_pnls: List[float] = []
    is_pnls: List[float] = []
    oos_trades = []
    cursor = base.start
    while cursor + timedelta(days=train_days + test_days) <= base.end + timedelta(days=1):
        tr_s, tr_e = cursor, cursor + timedelta(days=train_days - 1)
        te_s, te_e = tr_e + timedelta(days=1), tr_e + timedelta(days=test_days)
        best, best_score, best_rep = None, None, None
        for g in grid:
            rep = bt.run(replace(base, start=tr_s, end=tr_e, **g))
            exp = rep.metrics.get("expectancy")
            if rep.metrics.get("trades", 0) < min_train_trades or exp is None:
                continue
            if best_score is None or exp > best_score:
                best, best_score, best_rep = g, exp, rep
        fold = {"train": [tr_s.isoformat(), tr_e.isoformat()], "test": [te_s.isoformat(), te_e.isoformat()],
                "selected": best, "train_expectancy": best_score}
        if best is not None:
            test = bt.run(replace(base, start=te_s, end=te_e, **best))
            is_pnls += [t.pnl for t in best_rep.trades]
            oos_pnls += [t.pnl for t in test.trades]
            oos_trades += test.trades
            fold["test_metrics"] = test.metrics
        else:
            fold["skipped"] = f"no parameter set produced ≥ {min_train_trades} train trades"
        folds.append(fold)
        cursor = te_s   # roll forward by one test window
        if test_days <= 0:
            break
    eq = [base.starting_balance]
    for p in oos_pnls:
        eq.append(eq[-1] + p)
    return {
        "folds": folds, "grid_size": len(grid), "train_days": train_days, "test_days": test_days,
        "oos_metrics": json_safe(compute_metrics(oos_pnls, eq, base.starting_balance)),
        "is_metrics": json_safe(compute_metrics(is_pnls, [base.starting_balance], base.starting_balance)),
        "oos_trades": [t.model_dump(mode="json") for t in oos_trades],
    }
