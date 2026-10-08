"""Backtest engine, walk-forward, independent validation, research and review-agent tests."""
from datetime import date, timedelta

import pytest

from conftest import verified_calendar
from qqq.backtest.data import InMemorySource, LookAheadError, PointInTimeView, SyntheticSource
from qqq.backtest.engine import Backtester, BacktestConfig
from qqq.backtest.walk_forward import walk_forward
from qqq.macro_calendar import MacroCalendar
from qqq.models import BacktestReport, BacktestTrade, Bar, ValidationReport
from qqq.research import ResearchAgent
from qqq.review import ChangeRejected, ReviewAgent
from qqq.rules import ExitRules, FeeModel, MarketAssumptions, RiskLimits
from qqq.store import Store
from qqq.validation import SelfValidationError, ValidationAgent


def make_bars(n=330, start=date(2024, 1, 1), px=400.0, drift=0.6):
    out, d, i = [], start, 0
    while len(out) < n:
        if d.weekday() < 5:
            wob = 3.0 if i % 7 < 3 else -2.0
            c = px + drift * i + wob
            out.append(Bar(day=d, open=c, high=c + 2, low=c - 2, close=c, volume=1e6))
            i += 1
        d += timedelta(days=1)
    return out


BARS = make_bars()
IV = {b.day: 0.22 for b in BARS}
SRC = SyntheticSource(BARS, IV, MarketAssumptions())
START, END = BARS[205].day, BARS[-1].day
WHAT_IF = RiskLimits.what_if(max_loss_usd=1000.0)
CAL = verified_calendar()


def test_risk_limits_cannot_be_constructed_looser():
    with pytest.raises(ValueError):
        RiskLimits(max_loss_usd=100)
    assert RiskLimits.what_if(max_loss_usd=100).sandbox


def test_sandbox_limits_refused_by_proposal_path_engine():
    from qqq.risk_engine import RiskEngine
    with pytest.raises(ValueError):
        RiskEngine(WHAT_IF, FeeModel(), ExitRules())


def test_point_in_time_guard_blocks_future_chain_and_bars():
    view = PointInTimeView(SRC)
    view.cursor = BARS[210].day
    with pytest.raises(LookAheadError):
        view.chain(BARS[211].day)

    class Leaky(InMemorySource):
        def bars_through(self, day):
            return self.bars          # returns the future too
    leaky = PointInTimeView(Leaky(BARS, {}))
    leaky.cursor = BARS[210].day
    with pytest.raises(LookAheadError):
        leaky.bars()


def test_backtest_requires_verified_macro_history():
    bt = Backtester(SRC, RiskLimits(), FeeModel(), ExitRules(), MacroCalendar([], {}))
    with pytest.raises(ValueError, match="macro calendar"):
        bt.run(BacktestConfig(start=START, end=END))


def test_user_rules_produce_no_trades_on_qqq_like_chain():
    """Documents the $50-cap conflict: 0.10–0.15Δ $1–$5 wide spreads never pass."""
    rep = Backtester(SRC, RiskLimits(), FeeModel(), ExitRules(), CAL).run(BacktestConfig(start=START, end=END))
    assert rep.metrics["trades"] == 0
    assert rep.synthetic


def test_what_if_backtest_mechanics():
    rep = Backtester(SRC, WHAT_IF, FeeModel(), ExitRules(), CAL).run(
        BacktestConfig(start=START, end=END, fill_lag_days=1))
    assert rep.metrics["trades"] > 0
    assert rep.params["sandbox_limits"] is True
    for t in rep.trades:
        assert t.entry_day > START                         # filled the day after a signal at the earliest
        assert t.exit_day >= t.entry_day
        assert t.pnl >= -(t.max_loss_at_entry + 0.01)
        assert t.fees > 0 or t.exit_reason == "expiration"
        assert 0 < t.entry_credit < abs(t.short_strike - t.long_strike)
        assert 0.10 <= abs(t.short_delta) <= 0.15
    assert {t.exit_reason for t in rep.trades} <= {"profit_target", "delta_flag", "pre_event_exit", "expiration"}


def test_slippage_reduces_results():
    lo = Backtester(SRC, WHAT_IF, FeeModel(), ExitRules(), CAL).run(
        BacktestConfig(start=START, end=END, slippage_fraction=0.25))
    hi = Backtester(SRC, WHAT_IF, FeeModel(), ExitRules(), CAL).run(
        BacktestConfig(start=START, end=END, slippage_fraction=1.0))
    if lo.trades and hi.trades:
        assert sum(t.entry_credit for t in hi.trades) / len(hi.trades) <= \
            sum(t.entry_credit for t in lo.trades) / len(lo.trades) + 1e-9


def test_missing_greeks_are_never_backfilled():
    chains = {}
    for b in BARS[200:260]:
        qs = SRC.chain_on(b.day)
        chains[b.day] = [q.model_copy(update={"delta": None, "gamma": None, "theta": None, "vega": None,
                                              "greeks_source": "missing"}) for q in qs]
    src = InMemorySource(BARS[:260], chains, name="no_greeks")
    rep = Backtester(src, WHAT_IF, FeeModel(), ExitRules(), CAL).run(
        BacktestConfig(start=BARS[205].day, end=BARS[259].day))
    assert rep.metrics["trades"] == 0
    assert rep.days_missing_greeks > 0


def test_band_parameters_enforced():
    bt = Backtester(SRC, RiskLimits(), FeeModel(), ExitRules(), CAL)
    with pytest.raises(ValueError):
        bt.run(BacktestConfig(start=START, end=END, delta_target=0.25))
    with pytest.raises(ValueError):
        bt.run(BacktestConfig(start=START, end=END, delta_exit=0.50))


def test_walk_forward_rejects_risk_limit_tuning_and_runs():
    bt = Backtester(SRC, WHAT_IF, FeeModel(), ExitRules(), CAL)
    base = BacktestConfig(start=START, end=END)
    with pytest.raises(ValueError, match="not allowed"):
        walk_forward(bt, base, [{"max_loss_usd": 500}])
    wf = walk_forward(bt, base, [{"delta_target": 0.12}], train_days=60, test_days=30, min_train_trades=1)
    assert wf["folds"] and "oos_metrics" in wf
    for f in wf["folds"]:
        assert f["train"][1] < f["test"][0]                 # no overlap


# ── validation ────────────────────────────────────────────────────────────────

def _trade(pnl, i):
    d = date(2023, 1, 2) + timedelta(days=7 * i)
    return BacktestTrade(strategy="bull_put", entry_day=d, exit_day=d + timedelta(days=5),
                         expiry=d + timedelta(days=16), short_strike=300, long_strike=299, contracts=1,
                         entry_credit=0.6, exit_debit=0.3, fees=2.8, pnl=pnl, exit_reason="profit_target",
                         max_loss_at_entry=42.8)


def _report(pnls, oos_pnls, **kw):
    from qqq.backtest.metrics import compute_metrics, json_safe
    trades = [_trade(p, i) for i, p in enumerate(pnls)]
    eq, eqc = [250.0], [["2023-01-01", 250.0]]
    for i, p in enumerate(pnls):
        eq.append(eq[-1] + p)
        eqc.append([f"d{i}", eq[-1]])
    base = dict(id="bt-1", strategy_version="v1", proposer="strategy_research_agent",
                data_source="alphavantage_historical_options", synthetic=False, lookahead_guard=True,
                fill_model="mid_plus_slippage", slippage_fraction=0.5, fill_lag_days=1, macro_filter=True,
                start=date(2023, 1, 1), end=date(2025, 1, 1), params={"starting_balance": 250.0},
                trades=trades, equity_curve=eqc, days_total=500, days_missing_data=10, days_missing_greeks=0,
                metrics=json_safe(compute_metrics(pnls, eq, 250.0)),
                walk_forward={"grid_size": 2, "is_metrics": {"expectancy": 8.0},
                              "oos_trades": [_trade(p, i).model_dump(mode="json") for i, p in enumerate(oos_pnls)]})
    base.update(kw)
    return BacktestReport(**base)


GOOD = ([27.2] * 5 + [-42.8]) * 6      # losses interleaved → realistic drawdown


def test_validator_approves_only_sufficient_real_evidence():
    v = ValidationAgent().validate(_report(GOOD, GOOD))
    assert v.status == "APPROVED", v.reasons


@pytest.mark.parametrize("kw,needle", [
    ({"synthetic": True}, "SYNTHETIC"),
    ({"lookahead_guard": False}, "look-ahead"),
    ({"slippage_fraction": 0.0}, "optimistic"),
    ({"macro_filter": False}, "macro"),
    ({"days_missing_data": 200}, "missing data"),
    ({"walk_forward": None}, "out-of-sample"),
    ({"params": {"starting_balance": 250.0, "sandbox_limits": True}}, "WHAT-IF"),
])
def test_validator_rejections(kw, needle):
    v = ValidationAgent().validate(_report(GOOD, GOOD, **kw))
    assert v.status == "REJECTED" and any(needle in r for r in v.reasons), v.reasons


def test_validator_rejects_too_few_or_losing_oos_trades():
    assert ValidationAgent().validate(_report(GOOD, GOOD[:10])).status == "REJECTED"
    assert ValidationAgent().validate(_report(GOOD, [-10.0] * 40)).status == "REJECTED"


def test_validator_detects_overfit_degradation():
    rep = _report(GOOD, [1.0] * 40)     # IS expectancy 8 vs OOS 1
    v = ValidationAgent().validate(rep)
    assert any("overfit" in r for r in v.reasons)


def test_validator_recomputes_metrics():
    rep = _report(GOOD, GOOD)
    rep.metrics["net_pnl"] = 99999
    assert any("does not match" in r for r in ValidationAgent().validate(rep).reasons)


def test_validator_catches_impossible_fills():
    rep = _report(GOOD, GOOD)
    rep.trades[0] = rep.trades[0].model_copy(update={"entry_credit": 1.5})
    assert any("impossible fill" in r for r in ValidationAgent().validate(rep).reasons)


def test_proposer_cannot_validate_itself():
    with pytest.raises(SelfValidationError):
        ValidationAgent().validate(_report(GOOD, GOOD), requested_by="strategy_research_agent")
    with pytest.raises(SelfValidationError):
        ValidationAgent().validate(_report(GOOD, GOOD, proposer="independent_validation_agent"))


# ── research + review ─────────────────────────────────────────────────────────

def test_research_log_is_versioned_and_hypotheses_have_falsification():
    st = Store(":memory:")
    ra = ResearchAgent(st)
    assert all(h["falsification"] and h["edge_rationale"] for h in ra.hypotheses())
    e1, e2 = ra.log("note", "a"), ra.log("note", "b")
    assert (e1["version"], e2["version"]) == (1, 2)
    res = ra.evaluate(_report(GOOD, GOOD), "APPROVED")
    assert {r["hypothesis"] for r in res} >= {"H1-trend-vrp", "H2-iv-regime"}


def test_review_agent_cannot_loosen_risk_or_self_activate():
    st = Store(":memory:")
    rv = ReviewAgent(st, ResearchAgent(st))
    with pytest.raises(ChangeRejected):
        rv.propose_change({"max_loss_usd": 100}, "more trades", "v1")
    chg = rv.propose_change({"delta_target": 0.13}, "test", "v1")
    assert chg["status"] == "requires_independent_backtest"
    with pytest.raises(ChangeRejected, match="validation"):
        rv.activate_change(chg["id"], None, "human:jeff")
    approved = ValidationReport(id="val-1", backtest_id="bt", strategy_version="v1", validator="x",
                                proposer="y", status="APPROVED", reasons=[], warnings=[], metrics={})
    with pytest.raises(ChangeRejected, match="human"):
        rv.activate_change(chg["id"], approved, "review_agent")
    wrong = approved.model_copy(update={"strategy_version": "v2"})
    with pytest.raises(ChangeRejected, match="different strategy"):
        rv.activate_change(chg["id"], wrong, "human:jeff")
    assert rv.activate_change(chg["id"], approved, "human:jeff")["status"] == "active"
    assert st.kv_get("strategy_params") == {"delta_target": 0.13}
