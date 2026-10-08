"""Performance metrics. Computed independently by the validator from raw trades/equity."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence


def compute_metrics(pnls: Sequence[float], equity: Sequence[float], starting_balance: float) -> Dict[str, Any]:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    out: Dict[str, Any] = {
        "trades": n,
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "expectancy": round(sum(pnls) / n, 4) if n else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": (round(gross_win / gross_loss, 4) if gross_loss > 0
                          else (None if not wins else float("inf"))),
        "largest_loss": round(min(pnls), 2) if pnls else None,
    }
    out.update(equity_stats(equity, starting_balance))
    return out


def equity_stats(equity: Sequence[float], starting_balance: float) -> Dict[str, Any]:
    if len(equity) < 2:
        return {"sharpe": None, "max_drawdown": None, "max_drawdown_pct": None,
                "total_return_pct": None}
    rets = [(b / a - 1) for a, b in zip(equity[:-1], equity[1:]) if a > 0]
    sharpe = None
    if len(rets) > 1:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = round(mu / sd * math.sqrt(252), 3) if sd > 0 else None
    peak, mdd, mdd_pct = equity[0], 0.0, 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
        if peak > 0:
            mdd_pct = max(mdd_pct, (peak - v) / peak)
    return {"sharpe": sharpe, "max_drawdown": round(mdd, 2), "max_drawdown_pct": round(mdd_pct, 4),
            "total_return_pct": round(equity[-1] / starting_balance - 1, 4)}


def json_safe(m: Dict[str, Any]) -> Dict[str, Any]:
    return {k: ("inf" if isinstance(v, float) and math.isinf(v) else v) for k, v in m.items()}
