"""Unit tests: Greeks, indicators/trend, macro deadlines, data validation, screening, store, broker."""
import math
from datetime import date, datetime, time, timedelta

import pytest

from conftest import ET, NOW, good_spread, quote, verified_calendar
from qqq import indicators as ind
from qqq.greeks import bsm, implied_vol
from qqq.macro_calendar import MacroCalendar
from qqq.market_data import AlphaVantageProvider, DataUnavailable, validate_bars, validate_quote
from qqq.models import (Bar, ExitPlan, MacroEvent, MacroKind, OptionChain, TradeProposal, Trend,
                        RiskDecision)
from qqq.paper_broker import OrderRejected, PaperBroker
from qqq.rules import DataQualityRules, ExitRules, FeeModel, RiskLimits
from qqq.screening import build_candidates
from qqq.store import Store


# ── Greeks ────────────────────────────────────────────────────────────────────

def test_put_call_parity_and_iv_roundtrip():
    s, k, t, v, r, q = 750, 720, 15 / 365, 0.20, 0.04, 0.006
    c, p = bsm(s, k, t, v, r, q, "C"), bsm(s, k, t, v, r, q, "P")
    assert c.price - p.price == pytest.approx(s * math.exp(-q * t) - k * math.exp(-r * t), abs=1e-8)
    assert implied_vol(p.price, s, k, t, r, q, "P") == pytest.approx(v, abs=1e-4)
    assert -0.2 < p.delta < -0.05 and p.gamma > 0 and p.theta < 0 and p.vega > 0


def test_iv_outside_bounds_returns_none():
    assert implied_vol(1000, 750, 720, 0.04, 0.04, 0.0, "P") is None


# ── Indicators / trend ────────────────────────────────────────────────────────

def test_sma_rsi_basic():
    vals = list(range(1, 301))
    assert ind.sma(vals, 50) == pytest.approx(sum(range(251, 301)) / 50)
    assert ind.rsi(vals, 14) == 100.0
    assert ind.sma(vals[:10], 50) is None


@pytest.mark.parametrize("price,s50,s200,trend", [
    (110, 100, 105, Trend.BULLISH), (90, 100, 95, Trend.BEARISH),
    (102, 100, 105, Trend.AMBIGUOUS), (100, 100, 95, Trend.AMBIGUOUS), (100, None, 95, Trend.AMBIGUOUS)])
def test_trend_classification(price, s50, s200, trend):
    assert ind.classify_trend(price, s50, s200)[0] == trend


# ── Macro calendar ────────────────────────────────────────────────────────────

def _ev(kind, d, t):
    return MacroEvent(kind=kind, at=datetime.combine(d, t, tzinfo=ET), source="https://example.gov")


def test_premarket_event_deadline_is_prior_close_minus_buffer():
    cal = verified_calendar()
    dl = cal.exit_deadline_for(_ev(MacroKind.CPI, date(2026, 10, 13), time(8, 30)), ExitRules())  # Tuesday
    assert dl == datetime(2026, 10, 12, 15, 30, tzinfo=ET)


def test_monday_premarket_event_rolls_back_to_friday_and_skips_holidays():
    cal = MacroCalendar([], {}, holidays={date(2026, 10, 9): "test holiday"})
    dl = cal.exit_deadline_for(_ev(MacroKind.NFP, date(2026, 10, 12), time(8, 30)), ExitRules())
    assert dl == datetime(2026, 10, 8, 15, 30, tzinfo=ET)


def test_intraday_fomc_deadline():
    dl = verified_calendar().exit_deadline_for(_ev(MacroKind.FOMC, date(2026, 10, 28), time(14, 0)), ExitRules())
    assert dl == datetime(2026, 10, 28, 13, 30, tzinfo=ET)


def test_calendar_file_is_fail_closed_by_default():
    cal = MacroCalendar.load()
    assert len(cal.coverage_gaps(date(2026, 11, 1))) == 3


def test_market_hours():
    cal = verified_calendar()
    assert cal.is_market_open(datetime(2026, 10, 7, 10, 0, tzinfo=ET))
    assert not cal.is_market_open(datetime(2026, 10, 7, 16, 0, tzinfo=ET))
    assert not cal.is_market_open(datetime(2026, 10, 10, 11, 0, tzinfo=ET))   # Saturday


# ── Data validation ───────────────────────────────────────────────────────────

def test_quote_validation_flags_problems():
    rules = DataQualityRules()
    assert validate_quote(quote(700), NOW, rules) == []
    assert any("stale" in i for i in validate_quote(quote(700, now=NOW - timedelta(hours=2)), NOW, rules))
    assert any("crossed" in i for i in validate_quote(quote(700, bid=1.2, ask=1.0), NOW, rules))
    assert any("one-sided" in i for i in validate_quote(quote(700, bid=0.0), NOW, rules))
    assert any("Greeks" in i for i in validate_quote(quote(700, gamma=None), NOW, rules))
    assert any("out of range" in i for i in validate_quote(quote(700, delta=0.2), NOW, rules))


def _bars(n, start=date(2025, 1, 1), px=500.0, step=0.5):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            c = px + step * len(out)
            out.append(Bar(day=d, open=c, high=c + 1, low=c - 1, close=c, volume=1e6))
        d += timedelta(days=1)
    return out


def test_bar_validation_requires_history_and_freshness():
    rules = DataQualityRules()
    bars = _bars(250)
    assert validate_bars(bars, bars[-1].day + timedelta(days=1), rules).ok
    assert not validate_bars(bars[:100], bars[99].day, rules).ok
    assert not validate_bars(bars, bars[-1].day + timedelta(days=10), rules).ok


def test_bar_model_rejects_inconsistent_ohlc():
    with pytest.raises(ValueError):
        Bar(day=date(2026, 1, 2), open=10, high=9, low=8, close=10)


def test_alpha_vantage_sample_data_detected(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"message": "ok", "data": [{"contractID": "XXYYZZ999999C00020000", "symbol": "XXYYZZ"}]}
    monkeypatch.setattr("qqq.market_data.httpx.get", lambda *a, **k: R())
    with pytest.raises(DataUnavailable, match="ARTIFICIAL"):
        AlphaVantageProvider(api_key="x").realtime_chain("QQQ")


def test_alpha_vantage_premium_message_detected(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"message": "This is a premium endpoint.", "data": []}
    monkeypatch.setattr("qqq.market_data.httpx.get", lambda *a, **k: R())
    with pytest.raises(DataUnavailable):
        AlphaVantageProvider(api_key="x").historical_chain("QQQ", date(2025, 1, 2))


def test_alpha_vantage_rows_without_greeks_are_not_backfilled():
    rows = [{"contractID": "QQQ1", "symbol": "QQQ", "expiration": "2026-10-23", "strike": "720",
             "type": "put", "bid": "2.05", "ask": "2.08", "implied_volatility": "0.2"}]
    q = AlphaVantageProvider.parse_rows(rows, NOW)[0]
    assert q.greeks_source == "missing" and q.delta is None


# ── Screening ─────────────────────────────────────────────────────────────────

def test_builder_respects_direction_band_and_dte():
    exp = NOW.date() + timedelta(days=16)
    quotes = [quote(k, delta=-d, expiry=exp, bid=b, ask=b + 0.03)
              for k, d, b in [(690, 0.08, 0.9), (695, 0.11, 1.4), (700, 0.14, 2.0), (705, 0.20, 2.8)]]
    quotes.append(quote(760, right="C", delta=0.12, expiry=exp))
    chain = OptionChain(underlying="QQQ", asof=NOW, spot=750, quotes=quotes)
    cands = build_candidates(chain, Trend.BULLISH, RiskLimits(), FeeModel(), NOW)
    assert cands and all(c.strategy == "bull_put" and c.right == "P" for c in cands)
    assert {c.short.strike for c in cands} <= {695, 700}
    assert all(c.long.strike < c.short.strike for c in cands)
    assert build_candidates(chain, Trend.AMBIGUOUS, RiskLimits(), FeeModel(), NOW) == []


def test_builder_limit_credit_is_natural_rounded_down():
    exp = NOW.date() + timedelta(days=16)
    chain = OptionChain(underlying="QQQ", asof=NOW, spot=750,
                        quotes=[quote(700, bid=2.149, ask=2.17, expiry=exp), quote(699, bid=2.0, ask=2.08, expiry=exp)])
    c = build_candidates(chain, Trend.BULLISH, RiskLimits(), FeeModel(), NOW)[0]
    assert c.limit_credit == 0.06


# ── Store / audit ─────────────────────────────────────────────────────────────

def test_audit_chain_detects_tampering():
    st = Store(":memory:")
    st.audit("a", "x", {"n": 1})
    st.audit("b", "y", {"n": 2})
    assert st.verify_audit()
    st._conn.execute("UPDATE audit SET payload='{\"n\":3}' WHERE seq=1")
    assert not st.verify_audit()


def test_store_insert_is_idempotent():
    st = Store(":memory:")
    assert st.insert("c", "1", {"a": 1})
    assert not st.insert("c", "1", {"a": 2})
    assert st.get("c", "1") == {"a": 1}


# ── Paper broker ──────────────────────────────────────────────────────────────

def _proposal(spread, pid="p1"):
    return TradeProposal(id=pid, expires_at=NOW + timedelta(minutes=15), env="paper", strategy_version="v",
                         strategy_validation="UNVALIDATED", spread={}, spread_model=spread, thesis=[],
                         invalidation=[], risk=RiskDecision(approved=True, checks=[], limits_version="x"),
                         exit_plan=ExitPlan(profit_target_debit=0.3, delta_warn=0.3, delta_urgent=0.35))


@pytest.fixture
def broker():
    return PaperBroker(Store(":memory:"), FeeModel())


def test_open_fill_close_and_reconcile(broker):
    s = good_spread()
    o = broker.submit_open(_proposal(s), s)
    o = broker.try_fill(o.client_order_id, s.short, s.long, NOW)
    assert o.status == "filled" and o.avg_fill_price == 0.60
    pos = broker.positions("open")[0]
    assert broker.cash() == pytest.approx(250 + 60 - 1.40)
    assert broker.available_cash() == pytest.approx(250 + 60 - 1.40 - 100)
    assert broker.reconcile()[0]
    close = broker.submit_close(_proposal(s, "x1"), pos, 0.30)
    broker.apply_fill(close.client_order_id, 1, 0.30, NOW)
    closed = broker.position(pos.id)
    assert closed.status == "closed"
    assert closed.realized_pnl == pytest.approx(30 - 2.80)
    assert broker.cash() == pytest.approx(250 + 30 - 2.80)
    assert broker.reconcile()[0]


def test_resubmitting_same_proposal_is_idempotent(broker):
    s = good_spread()
    a = broker.submit_open(_proposal(s), s)
    b = broker.submit_open(_proposal(s), s)
    assert a.client_order_id == b.client_order_id and len(broker.orders()) == 1


def test_duplicate_legs_rejected(broker):
    s = good_spread()
    broker.submit_open(_proposal(s, "a"), s)
    with pytest.raises(OrderRejected, match="duplicate"):
        broker.submit_open(_proposal(s, "b"), s)


def test_non_marketable_order_stays_working(broker):
    s = good_spread(credit=0.60)
    o = broker.submit_open(_proposal(s), s)
    worse_short = s.short.model_copy(update={"bid": 0.50})
    assert broker.try_fill(o.client_order_id, worse_short, s.long, NOW).status == "working"


def test_partial_fill_detected_and_overfill_blocked(broker):
    s = good_spread(credit=0.10, contracts=3)
    o = broker.submit_open(_proposal(s), s)
    o = broker.apply_fill(o.client_order_id, 1, 0.10, NOW)
    assert o.status == "partially_filled"
    ok, issues = broker.reconcile()
    assert not ok and any("partially filled" in i for i in issues)
    with pytest.raises(OrderRejected, match="over-fill"):
        broker.apply_fill(o.client_order_id, 5, 0.10, NOW)


def test_reconcile_detects_ledger_tampering(broker):
    s = good_spread()
    o = broker.submit_open(_proposal(s), s)
    broker.try_fill(o.client_order_id, s.short, s.long, NOW)
    broker.store.kv_set("paper_cash", 999.0)
    ok, issues = broker.reconcile()
    assert not ok and "cash mismatch" in issues[0]


def test_expiration_settlement(broker):
    s = good_spread()
    o = broker.submit_open(_proposal(s), s)
    broker.try_fill(o.client_order_id, s.short, s.long, NOW)
    pos = broker.positions("open")[0]
    assert PaperBroker.intrinsic_debit(pos, 699.5) == 0.5
    settled = broker.settle_expiration(pos, 760.0, NOW)
    assert settled.status == "closed" and settled.realized_pnl == pytest.approx(60 - 1.40)
