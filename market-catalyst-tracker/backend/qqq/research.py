"""
Strategy Research Agent.

Maintains hypotheses — each with WHY it might have an edge and HOW it would be
disproven — and evaluates them against backtest trades. It can PROPOSE a
parameter (e.g. a gamma-to-credit ceiling) but cannot activate anything:
activation requires an independent validation report and a human (review.py).
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from qqq import llm
from qqq.models import BacktestReport, utcnow
from qqq.store import Store

AGENT = "strategy_research_agent"
MIN_BUCKET = 15

SEED_HYPOTHESES: List[Dict[str, Any]] = [
    {
        "id": "H1-trend-vrp",
        "title": "Trend-filtered OTM credit spreads harvest the volatility risk premium",
        "edge_rationale": (
            "Index option implied volatility has historically exceeded subsequently realised "
            "volatility on average (the variance risk premium, e.g. Carr & Wu 2009). Selling "
            "0.10–0.15 delta wings in the direction of the 50/200-day trend aims to collect that "
            "premium while avoiding the side the trend is moving toward."),
        "falsification": (
            "Rejected if walk-forward out-of-sample expectancy after fees and slippage is ≤ 0 over "
            "≥ 30 trades, or if the independent validator rejects the evidence."),
        "status": "untested",
    },
    {
        "id": "H2-iv-regime",
        "title": "Entries when Nasdaq-100 implied vol is elevated have higher expectancy",
        "edge_rationale": ("Premium per unit of risk is larger when IV is high relative to its own "
                           "1-year history, if the premium over realised vol persists."),
        "falsification": (f"Rejected if, with ≥ {MIN_BUCKET} trades per bucket, OOS mean P&L of "
                          "IV-percentile ≥ 50 entries is not higher than < 50 entries."),
        "status": "untested",
    },
    {
        "id": "H3-gamma-to-credit",
        "title": "A gamma-to-credit ceiling removes the worst risk/reward entries",
        "edge_rationale": ("High short gamma per dollar of credit means losses accelerate quickly on "
                           "an adverse move relative to what the trade can earn."),
        "falsification": (f"No threshold is adopted unless the top-quartile gamma/credit bucket (≥ "
                          f"{MIN_BUCKET} trades) shows lower OOS expectancy than the rest AND an "
                          "independent backtest with the ceiling is validated."),
        "status": "untested",
    },
    {
        "id": "H4-theta-window",
        "title": "14–21 DTE captures theta decay efficiently relative to gamma risk",
        "edge_rationale": ("Theta accelerates into expiry while gamma also rises; the window trades "
                           "off decay against path risk."),
        "falsification": "Rejected if profit-target exits are not the dominant winning exit OOS.",
        "status": "untested",
    },
]


class ResearchAgent:
    name = AGENT

    def __init__(self, store: Store):
        self.store = store
        for h in SEED_HYPOTHESES:
            self.store.insert("hypotheses", h["id"], {**h, "created_at": utcnow().isoformat(),
                                                      "evidence": []})

    def hypotheses(self) -> List[Dict[str, Any]]:
        return self.store.list("hypotheses", 100, newest_first=False)

    def log(self, kind: str, text: str, data: Optional[Dict[str, Any]] = None, author: str = AGENT) -> Dict:
        version = self.store.count("research_log") + 1
        entry = {"version": version, "at": utcnow().isoformat(), "author": author, "kind": kind,
                 "text": text, "data": data or {}}
        self.store.insert("research_log", f"v{version:05d}", entry)
        self.store.audit(author, f"research.{kind}", {"version": version})
        return entry

    def evaluate(self, rep: BacktestReport, validation_status: Optional[str]) -> List[Dict[str, Any]]:
        """Evaluate hypotheses on OOS trades when available, else in-sample (clearly labelled)."""
        trades = (rep.walk_forward or {}).get("oos_trades")
        sample = "out_of_sample"
        if not trades:
            trades = [t.model_dump(mode="json") for t in rep.trades]
            sample = "in_sample_only"
        results = []
        for h in self.hypotheses():
            fn = getattr(self, f"_eval_{h['id'].split('-')[0].lower()}", None)
            if fn is None:      # e.g. AI-drafted hypotheses need a human-written test first
                continue
            res = fn(trades, rep, validation_status)
            res.update({"sample": sample, "backtest_id": rep.id, "synthetic": rep.synthetic,
                        "at": utcnow().isoformat()})
            if rep.synthetic:
                res["verdict"] = "not_evidence"
                res["note"] = "synthetic data — evaluation is a pipeline check only"
            h["evidence"] = (h.get("evidence") or [])[-19:] + [res]
            h["status"] = res["verdict"] if not rep.synthetic else h.get("status", "untested")
            self.store.upsert("hypotheses", h["id"], h)
            results.append({"hypothesis": h["id"], **res})
        self.log("hypothesis_evaluation", f"Evaluated {len(results)} hypotheses on {rep.id} ({sample})",
                 {"results": results})
        return results

    # ── individual evaluations ────────────────────────────────────────────────

    def _eval_h1(self, trades, rep, vstatus):
        n = len(trades)
        exp = statistics.mean(t["pnl"] for t in trades) if trades else None
        verdict = ("supported_pending_more_data" if vstatus == "APPROVED"
                   else "insufficient_evidence" if n < 30 else
                   ("rejected" if exp is not None and exp <= 0 else "unvalidated"))
        return {"verdict": verdict, "trades": n, "expectancy": exp, "validation": vstatus}

    def _eval_h2(self, trades, rep, vstatus):
        hi = [t["pnl"] for t in trades if (t.get("iv_percentile") or -1) >= 50]
        lo = [t["pnl"] for t in trades if t.get("iv_percentile") is not None and t["iv_percentile"] < 50]
        if len(hi) < MIN_BUCKET or len(lo) < MIN_BUCKET:
            return {"verdict": "insufficient_evidence", "n_high": len(hi), "n_low": len(lo)}
        mh, ml = statistics.mean(hi), statistics.mean(lo)
        return {"verdict": "supported" if mh > ml else "rejected", "mean_high_iv": mh, "mean_low_iv": ml,
                "n_high": len(hi), "n_low": len(lo)}

    def _eval_h3(self, trades, rep, vstatus):
        rows = sorted((t for t in trades if t.get("gamma_to_credit") is not None),
                      key=lambda t: t["gamma_to_credit"])
        if len(rows) < 4 * MIN_BUCKET:
            return {"verdict": "insufficient_evidence", "n": len(rows),
                    "note": f"need ≥ {4 * MIN_BUCKET} trades with gamma data to test quartiles"}
        q = len(rows) // 4
        top, rest = rows[-q:], rows[:-q]
        mt, mr = statistics.mean(t["pnl"] for t in top), statistics.mean(t["pnl"] for t in rest)
        out = {"n": len(rows), "top_quartile_mean": mt, "rest_mean": mr,
               "candidate_ceiling": rows[-q - 1]["gamma_to_credit"]}
        if mt < mr:
            out["verdict"] = "candidate_threshold_requires_independent_backtest"
            self.store.upsert("change_proposals", f"gamma-{rep.id}", {
                "id": f"gamma-{rep.id}", "kind": "risk_tightening", "param": "max_gamma_to_credit",
                "value": out["candidate_ceiling"], "evidence_backtest": rep.id,
                "status": "requires_independent_backtest", "proposed_by": AGENT,
                "created_at": utcnow().isoformat(),
                "note": "Tightening only. Apply via env QQQ_MAX_GAMMA_TO_CREDIT after validation."})
        else:
            out["verdict"] = "rejected"
        return out

    def _eval_h4(self, trades, rep, vstatus):
        winners = [t for t in trades if t["pnl"] > 0]
        if len(winners) < MIN_BUCKET:
            return {"verdict": "insufficient_evidence", "winners": len(winners)}
        pt = sum(1 for t in winners if t["exit_reason"] == "profit_target")
        return {"verdict": "supported" if pt / len(winners) >= 0.5 else "rejected",
                "profit_target_share_of_wins": pt / len(winners), "winners": len(winners)}

    # ── optional AI drafting ──────────────────────────────────────────────────

    def draft_new_hypothesis(self, context: str) -> Optional[Dict[str, Any]]:
        text = llm.draft("Propose ONE testable hypothesis for defined-risk QQQ credit spreads "
                         "(14–21 DTE, 0.10–0.15 delta, trend-filtered). Give: title, edge rationale, "
                         f"and an explicit falsification test.\nContext:\n{context}")
        if not text:
            return None
        hid = f"AI-{self.store.count('hypotheses') + 1:03d}"
        h = {"id": hid, "title": "AI-drafted (unreviewed)", "edge_rationale": text,
             "falsification": "to be specified by a human reviewer", "status": "draft_unreviewed",
             "ai_drafted": True, "created_at": utcnow().isoformat(), "evidence": []}
        self.store.insert("hypotheses", hid, h)
        self.log("ai_draft", f"AI drafted hypothesis {hid} (unreviewed)", {"id": hid})
        return h
