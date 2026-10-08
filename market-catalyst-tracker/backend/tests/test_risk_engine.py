"""
Independent risk-engine tests. These exercise the deterministic rules directly
and do not share fixtures or helpers with the strategy/proposal code.
"""
import dataclasses
from datetime import date, datetime, time, timedelta

import pytest

from conftest import ET, NOW, ctx, good_spread, quote, verified_calendar
from qqq.macro_calendar import MacroCalendar
from qqq.models import CreditSpread, DataQualityReport, MacroEvent, MacroKind, Trend
from qqq.risk_engine import RiskEngine
from qqq.rules import (HARD_MAX_LOSS_USD, ExitRules, FeeModel, RiskLimits, load_env,
                       load_risk_limits, LiveExecutionNotAuthorized)

FEES = FeeModel()          # 0.65 + 0.05 per contract → $2.80 round trip for a 1-lot vertical
ENGINE = RiskEngine(RiskLimits(), FEES, ExitRules())


def failed(dec):
    return {c.name for c in dec.checks if not c.passed}


def test_baseline_spread_is_approved():
    dec = ENGINE.evaluate_entry(good_spread(), ctx())
    assert dec.approved, failed(dec)
    assert good_spread().max_loss(FEES) == pytest.approx(42.80)


# ── $50 max loss including fees ───────────────────────────────────────────────

def test_max_loss_includes_round_trip_fees():
    # credit 0.52 → (1 − 0.52)×100 = 48.00 + 2.80 fees = 50.80 > 50 → reject
    dec = ENGINE.evaluate_entry(good_spread(credit=0.52), ctx())
    assert "max_loss_incl_fees" in failed(dec)


def test_max_loss_boundary_exactly_50_is_allowed():
    # credit 0.528 → 47.20 + 2.80 = 50.00
    s = good_spread(credit=0.528)
    assert s.max_loss(FEES) == pytest.approx(50.0)
    assert "max_loss_incl_fees" not in failed(ENGINE.evaluate_entry(s, ctx()))


def test_typical_real_world_one_dollar_spread_is_rejected():
    # Measured live 2026-10-08: 721/720P @ 15 DTE, natural credit $0.06 → max loss ≈ $96.80
    s = good_spread(credit=0.06)
    dec = ENGINE.evaluate_entry(s, ctx())
    assert not dec.approved and "max_loss_incl_fees" in failed(dec)


def test_multiple_contracts_scale_max_loss():
    dec = ENGINE.evaluate_entry(good_spread(contracts=2), ctx())
    assert "max_loss_incl_fees" in failed(dec)


def test_wider_spread_rejected():
    assert "max_loss_incl_fees" in failed(ENGINE.evaluate_entry(good_spread(width=2.0), ctx()))


# ── structure / defined risk ──────────────────────────────────────────────────

def test_naked_or_inverted_wing_rejected():
    s = good_spread()
    inverted = s.model_copy(update={"long": quote(701, bid=0.9, ask=0.95)})
    assert "defined_risk_long_wing" in failed(ENGINE.evaluate_entry(inverted, ctx()))


def test_mismatched_expiry_rejected():
    s = good_spread()
    diag = s.model_copy(update={"long": quote(699, bid=0.03, ask=0.05, expiry=s.short.expiry + timedelta(days=7))})
    assert "vertical_same_expiry_and_type" in failed(ENGINE.evaluate_entry(diag, ctx()))


def test_mixed_option_types_rejected():
    s = good_spread()
    mixed = s.model_copy(update={"long": quote(699, right="C", bid=0.03, ask=0.05, delta=0.1)})
    assert failed(ENGINE.evaluate_entry(mixed, ctx())) & {"vertical_same_expiry_and_type",
                                                          "strategy_matches_option_type"}


def test_non_qqq_underlying_rejected():
    s = good_spread()
    spy = s.model_copy(update={"short": s.short.model_copy(update={"underlying": "SPY"})})
    assert "underlying_is_qqq" in failed(ENGINE.evaluate_entry(spy, ctx()))


def test_credit_must_be_positive_and_below_width():
    assert "positive_credit" in failed(ENGINE.evaluate_entry(good_spread(credit=0.0), ctx()))


# ── trend ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("trend", [Trend.AMBIGUOUS, Trend.BEARISH])
def test_bull_put_requires_bullish_trend(trend):
    assert "trend_matches_strategy" in failed(ENGINE.evaluate_entry(good_spread(), ctx(trend=trend)))


def test_bear_call_requires_bearish_trend():
    short = quote(760, right="C", bid=0.65, ask=0.68, delta=0.12)
    long = quote(761, right="C", bid=0.03, ask=0.05, delta=0.10, gamma=0.005, theta=-0.15, vega=0.25)
    s = CreditSpread(strategy="bear_call", short=short, long=long, limit_credit=0.60, asof=NOW)
    assert ENGINE.evaluate_entry(s, ctx(trend=Trend.BEARISH)).approved
    assert "trend_matches_strategy" in failed(ENGINE.evaluate_entry(s, ctx(trend=Trend.BULLISH)))


# ── expiration / Greeks ───────────────────────────────────────────────────────

@pytest.mark.parametrize("dte,ok", [(0, False), (13, False), (14, True), (21, True), (22, False)])
def test_dte_window(dte, ok):
    exp = NOW.date() + timedelta(days=dte)
    s = good_spread()
    s = s.model_copy(update={"short": s.short.model_copy(update={"expiry": exp}),
                             "long": s.long.model_copy(update={"expiry": exp})})
    names = failed(ENGINE.evaluate_entry(s, ctx()))
    assert ("dte_window" not in names) == ok
    if dte == 0:
        assert "not_0dte" in names


@pytest.mark.parametrize("delta,ok", [(-0.09, False), (-0.10, True), (-0.15, True), (-0.16, False)])
def test_short_delta_band(delta, ok):
    assert ("short_delta_band" not in failed(ENGINE.evaluate_entry(good_spread(delta=delta), ctx()))) == ok


def test_missing_greeks_rejected():
    s = good_spread()
    s = s.model_copy(update={"short": s.short.model_copy(update={"gamma": None, "greeks_source": "missing"})})
    assert "greeks_present" in failed(ENGINE.evaluate_entry(s, ctx()))


def test_negative_theta_rejected():
    s = good_spread(theta=-0.01)   # short decays slower than long → net theta negative
    assert s.net_theta() < 0
    assert "positive_net_theta" in failed(ENGINE.evaluate_entry(s, ctx()))


def test_gamma_threshold_unvalidated_warns_but_configured_threshold_enforces():
    dec = ENGINE.evaluate_entry(good_spread(), ctx())
    assert any("UNVALIDATED" in w for w in dec.warnings)
    strict = RiskEngine(dataclasses.replace(RiskLimits(), max_gamma_to_credit=1e-6), FEES, ExitRules())
    assert "gamma_to_credit" in failed(strict.evaluate_entry(good_spread(), ctx()))


# ── global controls ───────────────────────────────────────────────────────────

def test_kill_switch_blocks():
    assert "kill_switch_off" in failed(ENGINE.evaluate_entry(good_spread(), ctx(kill_switch_engaged=True)))


def test_only_one_position_or_working_order():
    assert "max_open_positions" in failed(ENGINE.evaluate_entry(good_spread(), ctx(open_positions=1)))
    assert "max_open_positions" in failed(ENGINE.evaluate_entry(good_spread(), ctx(working_orders=1)))


def test_daily_loss_limit():
    assert "daily_loss_limit" in failed(ENGINE.evaluate_entry(good_spread(), ctx(realized_pnl_today=-50.0)))
    assert "daily_loss_limit" not in failed(ENGINE.evaluate_entry(good_spread(), ctx(realized_pnl_today=-49.99)))


def test_unreconciled_broker_blocks():
    assert "broker_reconciled" in failed(ENGINE.evaluate_entry(good_spread(), ctx(reconciliation_ok=False)))


def test_bad_data_blocks():
    dq = DataQualityReport(ok=False, issues=["stale"])
    assert "data_quality_ok" in failed(ENGINE.evaluate_entry(good_spread(), ctx(data_quality=dq)))


def test_insufficient_cash_blocks():
    assert "cash_covers_max_loss" in failed(ENGINE.evaluate_entry(good_spread(), ctx(available_cash=40.0)))


def test_live_env_rejected():
    assert "environment_allows_paper_only" in failed(ENGINE.evaluate_entry(good_spread(), ctx(env="live")))


# ── macro events ──────────────────────────────────────────────────────────────

def test_unverified_macro_calendar_blocks():
    dec = ENGINE.evaluate_entry(good_spread(), ctx(calendar=MacroCalendar([], {})))
    assert "macro_calendar_verified" in failed(dec)


def test_calendar_verified_short_of_expiry_blocks():
    cal = verified_calendar(through=NOW.date() + timedelta(days=5))
    assert "macro_calendar_verified" in failed(ENGINE.evaluate_entry(good_spread(), ctx(calendar=cal)))


def test_event_too_soon_blocks_entry():
    # CPI tomorrow 08:30 → deadline today 15:30, only 4.5h away (< 6.5h minimum)
    ev = MacroEvent(kind=MacroKind.CPI, at=datetime.combine(NOW.date() + timedelta(days=1), time(8, 30), tzinfo=ET),
                    source="https://www.bls.gov/schedule/news_release/cpi.htm")
    dec = ENGINE.evaluate_entry(good_spread(), ctx(calendar=verified_calendar([ev])))
    assert "time_before_event_exit" in failed(dec)


def test_event_far_enough_allows_entry():
    ev = MacroEvent(kind=MacroKind.FOMC, at=datetime.combine(NOW.date() + timedelta(days=6), time(14, 0), tzinfo=ET),
                    source="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
    assert ENGINE.evaluate_entry(good_spread(), ctx(calendar=verified_calendar([ev]))).approved


# ── fail closed / immutability ────────────────────────────────────────────────

def test_exception_during_evaluation_rejects():
    class Boom:
        def coverage_gaps(self, *_):
            raise RuntimeError("calendar exploded")
    dec = ENGINE.evaluate_entry(good_spread(), ctx(calendar=Boom()))
    assert not dec.approved and "risk_engine_error" in failed(dec)


def test_limits_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        ENGINE.limits.max_loss_usd = 1000


def test_env_cannot_loosen_limits(monkeypatch):
    monkeypatch.setenv("QQQ_MAX_LOSS_USD", "500")
    monkeypatch.setenv("QQQ_SHORT_DELTA_MAX", "0.40")
    monkeypatch.setenv("QQQ_DAILY_LOSS_LIMIT_USD", "1000")
    lim = load_risk_limits()
    assert lim.max_loss_usd == HARD_MAX_LOSS_USD
    assert lim.short_delta_max == 0.15
    assert lim.daily_loss_limit_usd == 50.0


def test_env_can_tighten_limits(monkeypatch):
    monkeypatch.setenv("QQQ_MAX_LOSS_USD", "30")
    assert load_risk_limits().max_loss_usd == 30


def test_live_env_refused_at_config(monkeypatch):
    monkeypatch.setenv("QQQ_ENV", "live")
    with pytest.raises(LiveExecutionNotAuthorized):
        load_env()


def test_engine_has_no_override_api():
    public = {n for n in dir(ENGINE) if not n.startswith("_")}
    assert not public & {"override", "force", "skip", "set_limits", "disable"}


# ── exit signals ──────────────────────────────────────────────────────────────

def _pos(**kw):
    from qqq.models import PaperPosition
    base = dict(id="p1", strategy="bull_put", short_symbol="S", long_symbol="L",
                expiry=NOW.date() + timedelta(days=16), short_strike=700, long_strike=699, right="P",
                contracts=1, entry_credit=0.60, entry_fees=1.40, opened_at=NOW)
    base.update(kw)
    return PaperPosition(**base)


def test_exit_profit_target_at_50pct():
    r = ENGINE.exit_signals(_pos(), 0.30, -0.10, NOW, verified_calendar())
    assert any(x.startswith("profit_target") for x in r)
    r = ENGINE.exit_signals(_pos(), 0.31, -0.10, NOW, verified_calendar())
    assert not any(x.startswith("profit_target") for x in r)


@pytest.mark.parametrize("delta,flag", [(-0.29, None), (-0.30, "delta_warning"), (-0.35, "delta_urgent")])
def test_exit_delta_flags(delta, flag):
    r = ENGINE.exit_signals(_pos(), 0.5, delta, NOW, verified_calendar())
    hits = [x for x in r if x.startswith("delta")]
    assert (hits[0].split(":")[0] if hits else None) == flag


def test_exit_event_deadline_flag():
    ev = MacroEvent(kind=MacroKind.NFP, at=datetime.combine(NOW.date() + timedelta(days=1), time(8, 30), tzinfo=ET),
                    source="https://www.bls.gov/schedule/news_release/empsit.htm")
    late = datetime.combine(NOW.date(), time(15, 0), tzinfo=ET)
    r = ENGINE.exit_signals(_pos(), 0.5, -0.12, late, verified_calendar([ev]))
    assert any(x.startswith("event_deadline") for x in r)


def test_inconsistent_leg_quotes_rejected():
    s = good_spread()
    bad = s.model_copy(update={"long": s.long.model_copy(update={"delta": -0.20})})   # wing "riskier" than short
    assert "legs_quote_consistent" in failed(ENGINE.evaluate_entry(bad, ctx()))


def test_measured_real_world_negative_theta_case_rejected():
    """IBKR 2026-10-08 delayed snapshots: 721P θ −0.20528, 720P θ −0.20680 → net θ < 0."""
    short = quote(721, bid=2.14, ask=2.17, delta=-0.13318, gamma=0.0068462, theta=-0.20528, vega=0.31613)
    long = quote(720, bid=2.05, ask=2.08, delta=-0.12799, gamma=0.0066057, theta=-0.20680, vega=0.31423)
    s = CreditSpread(strategy="bull_put", short=short, long=long, limit_credit=0.06, asof=NOW)
    names = failed(ENGINE.evaluate_entry(s, ctx()))
    assert {"positive_net_theta", "max_loss_incl_fees"} <= names
