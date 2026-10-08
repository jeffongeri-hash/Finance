"""
End-to-end: fake market data → cycle → proposal → human approval (risk re-check)
→ paper fill → monitor → exit proposal → approval → closed position. Plus the
HTTP layer's authentication gate and fail-closed behaviour.
"""
from datetime import date, datetime, time, timedelta

import pytest

from conftest import ET, NOW, verified_calendar
from qqq.market_data import DataQualityRules, MarketDataAgent
from qqq.models import Bar, OptionQuote
from qqq.notifications import Notifier
from qqq.orchestrator import Orchestrator
from qqq.store import Store

EXP = NOW.date() + timedelta(days=16)


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.short_bid, self.short_ask = 0.65, 0.68
        self.long_bid, self.long_ask = 0.03, 0.05
        self.short_delta = -0.12
        self.fail = False

    def daily_bars(self, symbol, lookback_days):
        if self.fail:
            raise RuntimeError("feed down")
        out, d, i = [], NOW.date() - timedelta(days=400), 0
        while d < NOW.date():
            if d.weekday() < 5:
                c = 600 + 0.5 * i
                out.append(Bar(day=d, open=c, high=c + 1, low=c - 1, close=c, volume=1e6))
                i += 1
            d += timedelta(days=1)
        return out

    def spot(self, symbol):
        return self.daily_bars(symbol, 0)[-1].close + 0.5, NOW

    def expirations(self, symbol):
        return [NOW.date() + timedelta(days=3), EXP, NOW.date() + timedelta(days=30)]

    def option_chain(self, symbol, expiry, spot):
        mk = lambda k, b, a, dl, th: OptionQuote(underlying="QQQ", expiry=expiry, strike=k, right="P", bid=b, ask=a,
                                                 iv=0.2, delta=dl, gamma=0.006, theta=th, vega=0.3,
                                                 open_interest=500, quote_time=NOW, greeks_source="vendor")
        return [mk(700, self.short_bid, self.short_ask, self.short_delta, -0.20),
                mk(699, self.long_bid, self.long_ask, -0.10, -0.15),
                mk(690, 0.40, 0.45, -0.05, -0.10)]


@pytest.fixture
def orch():
    prov = FakeProvider()
    o = Orchestrator(store=Store(":memory:"), market=MarketDataAgent(prov, DataQualityRules()),
                     calendar=verified_calendar(), env="paper", notifier=Notifier(Store(":memory:"), ""),
                     clock=lambda: NOW)
    o.notify.store = o.store
    o.startup()
    o.provider = prov
    return o


def test_kill_switch_engaged_by_default_blocks_proposals(orch):
    cyc = orch.run_cycle("test")
    assert cyc["outcome"] == "NO_TRADE"
    assert cyc["rejection_summary"].get("kill_switch_off")


def test_full_paper_workflow(orch):
    orch.kill.disengage("test", "human:t", reconciliation_ok=True)
    cyc = orch.run_cycle("test")
    assert cyc["outcome"] == "PROPOSAL", cyc.get("rejection_summary") or cyc.get("message")
    pid = cyc["proposals"][0]
    prop = orch.store.get("proposals", pid)
    assert prop["status"] == "pending_approval" and prop["spread"]["max_loss"] <= 50
    assert prop["strategy_validation"] == "UNVALIDATED"
    assert any("UNVALIDATED" in w or "NO independently validated" in w for w in prop["warnings"])
    assert orch.broker.orders() == []                      # nothing executes without approval

    res = orch.approve(pid, "human:t")
    assert res["order"]["status"] == "filled"
    again = orch.approve(pid, "human:t")                   # idempotent replay
    assert again.get("idempotent_replay") and len(orch.broker.orders()) == 1
    assert len(orch.broker.positions("open")) == 1

    # A second cycle must not propose another position (one-position rule)
    cyc2 = orch.run_cycle("test")
    assert cyc2["outcome"] == "NO_TRADE"

    # Price drops: short delta hits 0.36 → urgent exit proposal
    orch.provider.short_delta, orch.provider.short_bid, orch.provider.short_ask = -0.36, 0.95, 1.00
    mon = orch.monitor("test")
    row = mon["positions"][0]
    assert any(r.startswith("delta_urgent") for r in row["exit_reasons"])
    exit_id = row["exit_proposal"]
    assert orch.monitor("test")["positions"][0].get("exit_proposal") is None    # no duplicate

    out = orch.approve(exit_id, "human:t")
    assert out["order"]["status"] == "filled"
    closed = orch.broker.positions("closed")[0]
    assert closed.realized_pnl == pytest.approx((0.60 - 0.97) * 100 - 2.80)
    assert orch.broker.reconcile()[0]
    assert orch.store.verify_audit()


def test_approval_rechecks_risk_with_fresh_quotes(orch):
    orch.kill.disengage("test", "human:t", reconciliation_ok=True)
    pid = orch.run_cycle("test")["proposals"][0]
    orch.provider.short_delta = -0.22                      # market moved: short leg out of band
    res = orch.approve(pid, "human:t")
    assert res["proposal"]["status"] == "rejected"
    assert "short_delta_band" in res["proposal"]["decision_note"]
    assert orch.broker.orders() == []


def test_non_marketable_order_rests_then_expires_end_of_day(orch):
    orch.kill.disengage("test", "human:t", reconciliation_ok=True)
    pid = orch.run_cycle("test")["proposals"][0]
    orch.provider.short_bid = 0.30                         # limit 0.60 no longer reachable
    res = orch.approve(pid, "human:t")
    assert res["order"]["status"] == "working"             # limit protects max loss; no worse fill
    assert orch.broker.positions("open") == []
    orch.clock = lambda: NOW + timedelta(days=1)
    orch.monitor("test")
    assert orch.broker.orders()[0].status == "cancelled"


def test_expired_proposal_cannot_be_approved(orch):
    orch.kill.disengage("test", "human:t", reconciliation_ok=True)
    pid = orch.run_cycle("test")["proposals"][0]
    orch.clock = lambda: NOW + timedelta(minutes=16)
    with pytest.raises(PermissionError, match="expired"):
        orch.approve(pid, "human:t")


def test_data_failure_means_no_trade(orch):
    orch.kill.disengage("test", "human:t", reconciliation_ok=True)
    orch.provider.fail = True
    cyc = orch.run_cycle("test")
    assert cyc["outcome"] == "NO_TRADE" and cyc["outcome_reason"] == "error"
    assert orch.store.list("alerts", 5)


def test_market_closed_means_no_trade(orch):
    orch.kill.disengage("test", "human:t", reconciliation_ok=True)
    orch.clock = lambda: datetime.combine(NOW.date(), time(18, 0), tzinfo=ET)
    cyc = orch.run_cycle("test")
    assert cyc["outcome"] == "NO_TRADE" and cyc["outcome_reason"] == "market_closed"


def test_reconciliation_mismatch_engages_kill_switch(orch):
    orch.kill.disengage("test", "human:t", reconciliation_ok=True)
    orch.store.kv_set("paper_cash", 1.0)
    orch.monitor("test")
    assert orch.kill.engaged()


# ── HTTP layer ────────────────────────────────────────────────────────────────

@pytest.fixture
def client(orch, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main, "_qqq", orch)
    return TestClient(main.app)       # no context manager → app startup hooks do not run


def test_reads_are_open_writes_need_auth(client, monkeypatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    assert client.get("/api/qqq/status").status_code == 200
    # TestClient's host is not loopback → protected routes refused without a token
    assert client.post("/api/qqq/cycle").status_code == 401
    assert client.post("/api/settings", json={"FRED_API_KEY": "x"}).status_code == 401
    assert client.post("/api/ibkr/order?account_id=a&symbol=QQQ&action=BUY&quantity=1").status_code == 401


def test_token_auth(client, monkeypatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret-token")
    assert client.post("/api/qqq/cycle", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.post("/api/qqq/cycle", headers={"Authorization": "Bearer s3cret-token"})
    assert r.status_code == 200 and r.json()["outcome"] == "NO_TRADE"     # kill switch still engaged


def test_kill_switch_api_and_proposal_flow(client, monkeypatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "t")
    h = {"Authorization": "Bearer t"}
    r = client.post("/api/qqq/kill-switch", json={"engaged": False, "reason": "ready", "by": "jeff"}, headers=h)
    assert r.status_code == 200 and r.json()["engaged"] is False
    cyc = client.post("/api/qqq/cycle", headers=h).json()
    assert cyc["outcome"] == "PROPOSAL"
    pid = cyc["proposals"][0]
    props = client.get("/api/qqq/proposals").json()
    assert props[0]["id"] == pid and "spread_model" not in props[0]
    r = client.post(f"/api/qqq/proposals/{pid}/approve", json={"approver": "jeff"}, headers=h)
    assert r.status_code == 200 and r.json()["order"]["status"] == "filled"
    assert len(client.get("/api/qqq/paper/positions").json()) == 1


def test_live_enable_no_longer_accepts_key_in_query(client, monkeypatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "t")
    r = client.post("/api/trading/live/enable?private_key=0xabc&funder=0xdef", headers={"Authorization": "Bearer t"})
    assert r.status_code in (400, 503)      # query params ignored; body/env required
