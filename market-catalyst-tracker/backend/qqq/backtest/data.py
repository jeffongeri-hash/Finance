"""
Historical data sources + a point-in-time guard that makes look-ahead impossible.

The engine never touches a source directly; it reads through `PointInTimeView`,
which raises `LookAheadError` for any bar or chain dated after the simulation
cursor. Sources that fabricate option prices (SyntheticSource) set
`synthetic = True`, and the validator refuses to approve such results.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Protocol
from zoneinfo import ZoneInfo

from qqq.greeks import bsm
from qqq.market_data import AlphaVantageProvider, DataUnavailable
from qqq.models import Bar, OptionQuote
from qqq.rules import UNDERLYING, MarketAssumptions

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


class LookAheadError(RuntimeError):
    pass


class HistoricalSource(Protocol):
    name: str
    synthetic: bool

    def trading_days(self, start: date, end: date) -> List[date]: ...
    def bars_through(self, day: date) -> List[Bar]: ...
    def chain_on(self, day: date) -> Optional[List[OptionQuote]]: ...


class PointInTimeView:
    def __init__(self, source: HistoricalSource):
        self._src = source
        self.cursor: Optional[date] = None

    def bars(self) -> List[Bar]:
        bars = self._src.bars_through(self.cursor)
        if bars and bars[-1].day > self.cursor:
            raise LookAheadError(f"source returned bar {bars[-1].day} after cursor {self.cursor}")
        return bars

    def chain(self, day: Optional[date] = None) -> Optional[List[OptionQuote]]:
        day = day or self.cursor
        if day > self.cursor:
            raise LookAheadError(f"requested chain for {day} while cursor is {self.cursor}")
        quotes = self._src.chain_on(day)
        if quotes and any(q.quote_time.astimezone(ET).date() > self.cursor for q in quotes):
            raise LookAheadError("chain contains quotes stamped after the cursor")
        return quotes


def eod(day: date) -> datetime:
    return datetime.combine(day, time(16, 0), tzinfo=ET)


# ── Sources ───────────────────────────────────────────────────────────────────

class InMemorySource:
    """Bars + per-day chains supplied directly (tests, or data you loaded yourself)."""

    def __init__(self, bars: List[Bar], chains: Dict[date, List[OptionQuote]],
                 name: str = "in_memory", synthetic: bool = False):
        self.bars = sorted(bars, key=lambda b: b.day)
        self.chains = chains
        self.name = name
        self.synthetic = synthetic

    def trading_days(self, start: date, end: date) -> List[date]:
        return [b.day for b in self.bars if start <= b.day <= end]

    def bars_through(self, day: date) -> List[Bar]:
        return [b for b in self.bars if b.day <= day]

    def chain_on(self, day: date) -> Optional[List[OptionQuote]]:
        return self.chains.get(day)


class AlphaVantageHistoricalSource:
    """Real end-of-day historical chains with vendor IV/Greeks (premium key).
    Responses are cached on disk so a backtest re-run costs no API calls."""
    synthetic = False

    def __init__(self, bars: List[Bar], cache_dir: Path, provider: Optional[AlphaVantageProvider] = None,
                 max_dte: int = 45):
        self.name = "alphavantage_historical_options"
        self.bars = sorted(bars, key=lambda b: b.day)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.provider = provider or AlphaVantageProvider()
        self.max_dte = max_dte

    def trading_days(self, start: date, end: date) -> List[date]:
        return [b.day for b in self.bars if start <= b.day <= end]

    def bars_through(self, day: date) -> List[Bar]:
        return [b for b in self.bars if b.day <= day]

    def chain_on(self, day: date) -> Optional[List[OptionQuote]]:
        f = self.cache_dir / f"{UNDERLYING}_{day.isoformat()}.json"
        if f.exists():
            rows = json.loads(f.read_text())
            return [OptionQuote(**r) for r in rows] or None
        try:
            quotes = self.provider.historical_chain(UNDERLYING, day)
        except DataUnavailable:
            raise
        except Exception as exc:
            logger.warning("historical chain %s failed: %s", day, exc)
            return None
        quotes = [q for q in quotes if 0 <= (q.expiry - day).days <= self.max_dte]
        f.write_text(json.dumps([q.model_dump(mode="json") for q in quotes]))
        return quotes or None


class CsvHistoricalSource:
    """Directory layout:
         bars.csv                 day,open,high,low,close,volume
         chains/YYYY-MM-DD.csv    expiration,strike,type,bid,ask,volume,open_interest,
                                  implied_volatility,delta,gamma,theta,vega[,contractID]
    Rows lacking Greeks are kept with greeks_source='missing' (never back-filled)."""
    synthetic = False

    def __init__(self, root: Path, name: str = "csv_historical"):
        self.root = Path(root)
        self.name = name
        with open(self.root / "bars.csv") as fh:
            self.bars = sorted((Bar(day=date.fromisoformat(r["day"]), open=float(r["open"]),
                                    high=float(r["high"]), low=float(r["low"]), close=float(r["close"]),
                                    volume=float(r.get("volume") or 0)) for r in csv.DictReader(fh)),
                               key=lambda b: b.day)

    def trading_days(self, start: date, end: date) -> List[date]:
        return [b.day for b in self.bars if start <= b.day <= end]

    def bars_through(self, day: date) -> List[Bar]:
        return [b for b in self.bars if b.day <= day]

    def chain_on(self, day: date) -> Optional[List[OptionQuote]]:
        f = self.root / "chains" / f"{day.isoformat()}.csv"
        if not f.exists():
            return None
        with open(f) as fh:
            rows = [{**r, "symbol": UNDERLYING} for r in csv.DictReader(fh)]
        return AlphaVantageProvider.parse_rows(rows, eod(day)) or None


class SyntheticSource:
    """BSM-priced chains from historical bars and a point-in-time IV series (e.g. VXN close
    on that same day). Exercises the pipeline only — results are labelled SYNTHETIC and
    can never be approved by the validator."""
    synthetic = True

    def __init__(self, bars: List[Bar], iv_by_day: Dict[date, float], mkt: MarketAssumptions,
                 half_spread: float = 0.03, strike_step: float = 1.0):
        self.name = "synthetic_bsm"
        self.bars = sorted(bars, key=lambda b: b.day)
        self.iv = iv_by_day
        self.mkt = mkt
        self.half_spread = half_spread
        self.step = strike_step

    def trading_days(self, start: date, end: date) -> List[date]:
        return [b.day for b in self.bars if start <= b.day <= end]

    def bars_through(self, day: date) -> List[Bar]:
        return [b for b in self.bars if b.day <= day]

    def chain_on(self, day: date) -> Optional[List[OptionQuote]]:
        bar = next((b for b in self.bars if b.day == day), None)
        iv = self.iv.get(day)
        if bar is None or not iv:
            return None
        spot = bar.close
        out = []
        lo, hi = int(spot * 0.85 / self.step), int(spot * 1.15 / self.step)
        # Weekly Friday expirations out to 35 days: always one inside any 14–21 DTE window,
        # and previously opened expiries stay listed so positions can be marked daily.
        expiries = [day + timedelta(days=k) for k in range(0, 36) if (day + timedelta(days=k)).weekday() == 4]
        for exp in expiries:
            t = ((exp - day).days or 0.5) / 365.0
            for i in range(lo, hi + 1):
                k = i * self.step
                for right in ("P", "C"):
                    g = bsm(spot, k, t, iv, self.mkt.risk_free_rate, self.mkt.dividend_yield, right)
                    bid = max(round(g.price - self.half_spread, 2), 0.0)
                    ask = round(g.price + self.half_spread, 2)
                    if ask < 0.05:
                        continue
                    out.append(OptionQuote(underlying=UNDERLYING, expiry=exp, strike=k, right=right,
                                           bid=bid, ask=ask, iv=iv, delta=g.delta, gamma=g.gamma,
                                           theta=g.theta, vega=g.vega, greeks_source="model_from_quote_iv",
                                           open_interest=1000, quote_time=eod(day)))
        return out
