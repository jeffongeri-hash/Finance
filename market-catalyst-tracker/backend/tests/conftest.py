import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqq.macro_calendar import MacroCalendar  # noqa: E402
from qqq.models import (CreditSpread, DataQualityReport, MacroEvent, MacroKind,  # noqa: E402
                        OptionQuote, Trend)
from qqq.risk_engine import RiskContext  # noqa: E402

ET = ZoneInfo("America/New_York")
# A Wednesday, mid-session.
NOW = datetime(2026, 10, 7, 11, 0, tzinfo=ET)


def quote(strike, right="P", bid=1.0, ask=1.05, delta=-0.12, gamma=0.006, theta=-0.20, vega=0.30,
          expiry=None, iv=0.20, oi=500, now=NOW, underlying="QQQ", source="vendor"):
    return OptionQuote(underlying=underlying, expiry=expiry or (now.date() + timedelta(days=16)),
                       strike=strike, right=right, bid=bid, ask=ask, iv=iv, delta=delta, gamma=gamma,
                       theta=theta, vega=vega, open_interest=oi, quote_time=now, greeks_source=source)


def good_spread(credit=0.60, width=1.0, contracts=1, now=NOW, **short_kw):
    """A bull put that passes every rule: max loss = (1 − 0.60)×100 + 2.80 fees = $42.80."""
    short = quote(700, bid=credit + 0.05, ask=credit + 0.08, **short_kw)
    long = quote(700 - width, bid=0.03, ask=0.05, delta=-0.10, gamma=0.005, theta=-0.15, vega=0.25)
    return CreditSpread(strategy="bull_put", short=short, long=long, contracts=contracts,
                        limit_credit=credit, asof=now)


def verified_calendar(events=None, through=date(2027, 12, 31)):
    return MacroCalendar(events or [], {k: through for k in MacroKind},
                         verified_from={k: date(2015, 1, 1) for k in MacroKind})


def ctx(**kw):
    base = dict(now=NOW, env="paper", trend=Trend.BULLISH, data_quality=DataQualityReport(ok=True),
                kill_switch_engaged=False, reconciliation_ok=True, open_positions=0, working_orders=0,
                realized_pnl_today=0.0, available_cash=250.0, calendar=verified_calendar(),
                strategy_validated=True)
    base.update(kw)
    return RiskContext(**base)


@pytest.fixture
def now():
    return NOW
