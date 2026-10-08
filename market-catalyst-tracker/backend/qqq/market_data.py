"""
Market Data Agent
=================
Collects QQQ daily candles, indicators, implied-volatility context and option
chains, then validates them. Any critical issue → DataQualityError → no trade.

Providers
---------
* YFinanceProvider     — free. Candles, ^VXN history, option chains (bid/ask,
                         volume, OI, IV). Yahoo supplies NO Greeks, so Greeks
                         are computed with BSM from each quote's own IV and
                         labelled `model_from_quote_iv`. Quotes are delayed.
* AlphaVantageProvider — REALTIME_OPTIONS (vendor Greeks) and HISTORICAL_OPTIONS
                         (used by the backtester). Both are premium endpoints;
                         a non-premium key returns artificial sample rows, which
                         are detected and rejected here.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Protocol, Tuple

import httpx
from zoneinfo import ZoneInfo

from qqq import indicators as ind
from qqq.greeks import bsm
from qqq.models import (Bar, DataQualityReport, OptionChain, OptionQuote,
                        UnderlyingSnapshot, utcnow)
from qqq.rules import UNDERLYING, DataQualityRules, MarketAssumptions

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


class DataQualityError(RuntimeError):
    def __init__(self, report: DataQualityReport):
        super().__init__("; ".join(report.issues))
        self.report = report


class DataUnavailable(RuntimeError):
    pass


class MarketDataProvider(Protocol):
    name: str

    def daily_bars(self, symbol: str, lookback_days: int) -> List[Bar]: ...
    def expirations(self, symbol: str) -> List[date]: ...
    def option_chain(self, symbol: str, expiry: date, spot: float) -> List[OptionQuote]: ...
    def spot(self, symbol: str) -> Tuple[float, datetime]: ...


# ── Greeks enrichment ─────────────────────────────────────────────────────────

def add_model_greeks(q: OptionQuote, spot: float, now: datetime, mkt: MarketAssumptions) -> OptionQuote:
    """Compute Greeks from the quote's OWN contemporaneous IV. Never back-fills historical data."""
    if q.has_greeks:
        return q
    if not q.iv or q.iv <= 0:
        return q.model_copy(update={"greeks_source": "missing"})
    expiry_close = datetime.combine(q.expiry, time(16, 0), tzinfo=ET)
    t = max((expiry_close - now).total_seconds(), 0) / (365.0 * 86400)
    if t <= 0:
        return q.model_copy(update={"greeks_source": "missing"})
    g = bsm(spot, q.strike, t, q.iv, mkt.risk_free_rate, mkt.dividend_yield, q.right)
    return q.model_copy(update={"delta": g.delta, "gamma": g.gamma, "theta": g.theta,
                                "vega": g.vega, "greeks_source": "model_from_quote_iv"})


# ── yfinance ──────────────────────────────────────────────────────────────────

class YFinanceProvider:
    name = "yfinance"

    def __init__(self, mkt: MarketAssumptions):
        self.mkt = mkt

    def _ticker(self, symbol: str):
        import yfinance as yf
        return yf.Ticker(symbol)

    def daily_bars(self, symbol: str, lookback_days: int = 420) -> List[Bar]:
        df = self._ticker(symbol).history(period=f"{lookback_days}d", interval="1d", auto_adjust=False)
        bars = []
        for idx, row in df.iterrows():
            try:
                bars.append(Bar(day=idx.date(), open=float(row["Open"]), high=float(row["High"]),
                                low=float(row["Low"]), close=float(row["Close"]),
                                volume=float(row.get("Volume", 0) or 0)))
            except Exception as exc:     # malformed bar → dropped and reported by validator
                logger.warning("dropping bad bar %s %s: %s", symbol, idx, exc)
        return bars

    def spot(self, symbol: str) -> Tuple[float, datetime]:
        fi = self._ticker(symbol).fast_info
        price = float(fi.last_price)
        return price, utcnow()

    def expirations(self, symbol: str) -> List[date]:
        return [date.fromisoformat(s) for s in self._ticker(symbol).options]

    def option_chain(self, symbol: str, expiry: date, spot: float) -> List[OptionQuote]:
        chain = self._ticker(symbol).option_chain(expiry.isoformat())
        now = utcnow()
        quotes: List[OptionQuote] = []
        for right, df in (("C", chain.calls), ("P", chain.puts)):
            for _, r in df.iterrows():
                try:
                    q = OptionQuote(
                        underlying=symbol, expiry=expiry, strike=float(r["strike"]), right=right,
                        bid=float(r.get("bid") or 0), ask=float(r.get("ask") or 0),
                        last=_f(r.get("lastPrice")), volume=_f(r.get("volume")),
                        open_interest=_f(r.get("openInterest")), iv=_f(r.get("impliedVolatility")),
                        quote_time=now, symbol=str(r.get("contractSymbol") or ""),
                    )
                    quotes.append(add_model_greeks(q, spot, now, self.mkt))
                except Exception as exc:
                    logger.debug("skip malformed option row: %s", exc)
        return quotes

    def vxn_history(self, lookback_days: int = 400) -> List[Bar]:
        """Cboe Nasdaq-100 Volatility Index — the credible historical IV comparison for QQQ."""
        return self.daily_bars("^VXN", lookback_days)


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


# ── Alpha Vantage ─────────────────────────────────────────────────────────────

class AlphaVantageProvider:
    """Option-chain source with vendor Greeks. Requires a PREMIUM key."""
    name = "alphavantage"
    BASE = "https://www.alphavantage.co/query"

    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0):
        self.api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY", "")
        self.timeout = timeout

    def _query(self, **params) -> Dict[str, Any]:
        if not self.api_key:
            raise DataUnavailable("ALPHA_VANTAGE_API_KEY is not set")
        r = httpx.get(self.BASE, params={**params, "apikey": self.api_key, "datatype": "json"},
                      timeout=self.timeout)
        r.raise_for_status()
        payload = r.json()
        msg = str(payload.get("message") or payload.get("Information") or payload.get("Note") or "")
        if "premium" in msg.lower() or "rate limit" in msg.lower():
            raise DataUnavailable(f"Alpha Vantage refused request: {msg[:160]}")
        rows = payload.get("data") or []
        if any(str(row.get("symbol", "")).upper() == "XXYYZZ" for row in rows):
            raise DataUnavailable("Alpha Vantage returned ARTIFICIAL sample data (non-premium key)")
        return payload

    @staticmethod
    def parse_rows(rows: List[Dict[str, Any]], quote_time: datetime) -> List[OptionQuote]:
        out = []
        for r in rows:
            try:
                greeks = {k: _f(r.get(k)) for k in ("delta", "gamma", "theta", "vega")}
                has = None not in greeks.values()
                out.append(OptionQuote(
                    underlying=str(r["symbol"]).upper(), expiry=date.fromisoformat(r["expiration"]),
                    strike=float(r["strike"]), right="C" if str(r["type"]).lower() == "call" else "P",
                    bid=float(r.get("bid") or 0), ask=float(r.get("ask") or 0),
                    last=_f(r.get("last")), volume=_f(r.get("volume")),
                    open_interest=_f(r.get("open_interest")), iv=_f(r.get("implied_volatility")),
                    **greeks, greeks_source="vendor" if has else "missing",
                    quote_time=quote_time, symbol=str(r.get("contractID") or ""),
                ))
            except Exception as exc:
                logger.debug("skip malformed AV row: %s", exc)
        return out

    def realtime_chain(self, symbol: str, expiry: Optional[date] = None) -> List[OptionQuote]:
        params = {"function": "REALTIME_OPTIONS", "symbol": symbol, "require_greeks": "true"}
        if expiry:
            params["expiration"] = expiry.isoformat()
        payload = self._query(**params)
        return self.parse_rows(payload.get("data") or [], utcnow())

    def historical_chain(self, symbol: str, day: date) -> List[OptionQuote]:
        payload = self._query(function="HISTORICAL_OPTIONS", symbol=symbol, date=day.isoformat())
        # End-of-day snapshot: timestamp it at that session's 16:00 ET close.
        qt = datetime.combine(day, time(16, 0), tzinfo=ET)
        return self.parse_rows(payload.get("data") or [], qt)


# ── Validation ────────────────────────────────────────────────────────────────

def validate_bars(bars: List[Bar], today: date, rules: DataQualityRules) -> DataQualityReport:
    issues, warnings = [], []
    if len(bars) < rules.min_history_bars:
        issues.append(f"only {len(bars)} daily bars; need ≥ {rules.min_history_bars} for SMA200")
    if bars:
        age = (today - bars[-1].day).days
        if age > rules.max_bar_age_days:
            issues.append(f"latest daily bar is {age} days old ({bars[-1].day})")
        days = [b.day for b in bars]
        if days != sorted(days) or len(set(days)) != len(days):
            issues.append("daily bars are unsorted or duplicated")
        for prev, cur in zip(bars[:-1], bars[1:]):
            if abs(cur.close / prev.close - 1) > 0.25:
                issues.append(f"implausible close-to-close move on {cur.day} (possible bad data/split)")
                break
    else:
        issues.append("no daily bars")
    return DataQualityReport(ok=not issues, issues=issues, warnings=warnings)


def validate_spot(spot: float, last_close: float, rules: DataQualityRules) -> List[str]:
    gap = abs(spot / last_close - 1)
    if gap > rules.max_spot_vs_close_gap_pct:
        return [f"spot {spot:.2f} deviates {gap:.1%} from last close {last_close:.2f} — inconsistent feed"]
    return []


def validate_quote(q: OptionQuote, now: datetime, rules: DataQualityRules) -> List[str]:
    issues = []
    age = (now - q.quote_time).total_seconds()
    if age > rules.max_quote_age_seconds:
        issues.append(f"{q.osi()} quote is stale ({int(age)}s old)")
    if age < -60:
        issues.append(f"{q.osi()} quote timestamp is in the future")
    if q.ask < q.bid:
        issues.append(f"{q.osi()} crossed market (bid {q.bid} > ask {q.ask})")
    if rules.require_two_sided_quote and (q.bid <= 0 or q.ask <= 0):
        issues.append(f"{q.osi()} one-sided/zero quote")
    if q.open_interest is not None and q.open_interest < rules.min_open_interest:
        issues.append(f"{q.osi()} open interest {q.open_interest:.0f} below minimum")
    if not q.has_greeks:
        issues.append(f"{q.osi()} Greeks unavailable")
    if q.iv is None or q.iv <= 0 or q.iv > 3:
        issues.append(f"{q.osi()} implied volatility missing or implausible ({q.iv})")
    if q.delta is not None:
        if q.right == "P" and not (-1.0 <= q.delta <= 0.0):
            issues.append(f"{q.osi()} put delta {q.delta} out of range")
        if q.right == "C" and not (0.0 <= q.delta <= 1.0):
            issues.append(f"{q.osi()} call delta {q.delta} out of range")
    return issues


# ── Agent ─────────────────────────────────────────────────────────────────────

class MarketDataAgent:
    name = "market_data_agent"

    def __init__(self, provider: MarketDataProvider, rules: DataQualityRules,
                 option_provider: Optional[AlphaVantageProvider] = None):
        self.provider = provider
        self.rules = rules
        self.option_provider = option_provider

    def snapshot(self, now: Optional[datetime] = None) -> Tuple[UnderlyingSnapshot, List[Bar]]:
        now = now or utcnow()
        bars = self.provider.daily_bars(UNDERLYING, 420)
        report = validate_bars(bars, now.date(), self.rules)
        if not report.ok:
            raise DataQualityError(report)
        spot, _ = self.provider.spot(UNDERLYING)
        spot_issues = validate_spot(spot, bars[-1].close, self.rules)
        if spot_issues:
            raise DataQualityError(DataQualityReport(ok=False, issues=spot_issues))
        snap = build_snapshot(bars, spot, now, source=self.provider.name)
        snap.iv_context = self.iv_context()
        return snap, bars

    def iv_context(self) -> Dict[str, Any]:
        vxn_fn = getattr(self.provider, "vxn_history", None)
        if not vxn_fn:
            return {"available": False, "reason": "provider has no VXN history"}
        try:
            vxn = vxn_fn(400)
            closes = [b.close for b in vxn][-252:]
            if len(closes) < 120:
                return {"available": False, "reason": f"only {len(closes)} VXN observations"}
            cur = closes[-1]
            return {"available": True, "source": "Cboe VXN (Nasdaq-100 Volatility Index) via yfinance",
                    "vxn": round(cur, 2), "percentile_1y": ind.percentile_rank(closes[:-1], cur),
                    "observations": len(closes), "asof": vxn[-1].day.isoformat()}
        except Exception as exc:
            return {"available": False, "reason": f"VXN fetch failed: {exc}"}

    def chain(self, spot: float, min_dte: int, max_dte: int, now: Optional[datetime] = None) -> OptionChain:
        now = now or utcnow()
        quotes: List[OptionQuote] = []
        exps = [e for e in self.provider.expirations(UNDERLYING)
                if min_dte <= (e - now.date()).days <= max_dte]
        if not exps:
            raise DataQualityError(DataQualityReport(
                ok=False, issues=[f"no expirations with {min_dte}–{max_dte} DTE"]))
        for e in exps:
            if self.option_provider is not None:
                try:
                    quotes += self.option_provider.realtime_chain(UNDERLYING, e)
                    continue
                except DataUnavailable as exc:
                    logger.info("Alpha Vantage unavailable, falling back to %s: %s", self.provider.name, exc)
            quotes += self.provider.option_chain(UNDERLYING, e, spot)
        if not quotes:
            raise DataQualityError(DataQualityReport(ok=False, issues=["option chain empty"]))
        return OptionChain(underlying=UNDERLYING, asof=now, spot=spot, quotes=quotes,
                           source=self.option_provider.name if self.option_provider else self.provider.name)


def build_snapshot(bars: List[Bar], spot: float, now: datetime, source: str = "") -> UnderlyingSnapshot:
    """Indicators use ONLY the supplied bars (callers pass bars ≤ the decision date)."""
    closes = [b.close for b in bars]
    s50, s200 = ind.sma(closes, 50), ind.sma(closes, 200)
    trend, reason = ind.classify_trend(spot, s50, s200)
    return UnderlyingSnapshot(
        symbol=UNDERLYING, asof=now, price=spot, last_bar_day=bars[-1].day,
        sma50=s50, sma200=s200, rsi14=ind.rsi(closes, 14), atr14=ind.atr(bars, 14),
        hv20=ind.historical_vol(closes, 20), bars=len(bars), trend=trend, trend_reason=reason,
        source=source,
    )
