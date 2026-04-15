"""
yfinance adapter — primary free data source.
Wraps all Yahoo Finance calls; returns plain dicts/lists so callers
can shape into Pydantic models independently.
"""
from __future__ import annotations
import time
import logging
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=8)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None and str(val) != "nan" else default
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val) if val is not None and str(val) != "nan" else default
    except (TypeError, ValueError):
        return default


# ── Quote ─────────────────────────────────────────────────────────────────────

def get_quote(symbol: str) -> Optional[Dict]:
    """Single symbol quote. Returns None on failure."""
    try:
        t = yf.Ticker(symbol)
        fi = t.fast_info
        prev_close = _safe_float(fi.previous_close)
        price = _safe_float(fi.last_price) or _safe_float(getattr(fi, "regular_market_price", None))
        if price == 0:
            return None
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0.0

        info = {}
        try:
            info = t.info or {}
        except Exception:
            pass

        return {
            "symbol": symbol,
            "name": info.get("shortName") or info.get("longName") or symbol,
            "price": round(price, 4),
            "change": round(change, 4),
            "change_pct": round(change_pct, 3),
            "volume": _safe_int(fi.three_month_average_volume),
            "avg_volume": _safe_int(fi.three_month_average_volume),
            "market_cap": _safe_float(fi.market_cap) or None,
            "sector": info.get("sector") or "",
        }
    except Exception as e:
        logger.debug("get_quote %s failed: %s", symbol, e)
        return None


def get_quotes_bulk(symbols: List[str]) -> List[Dict]:
    """Parallel bulk quotes — uses yf.download for efficiency then enriches."""
    results: List[Dict] = []

    # Fast path: yf.download gets OHLCV for all at once
    try:
        data = yf.download(
            symbols,
            period="2d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as e:
        logger.warning("bulk download failed: %s", e)
        data = pd.DataFrame()

    for sym in symbols:
        try:
            if len(symbols) == 1:
                sym_data = data
            else:
                sym_data = data[sym] if sym in data.columns.get_level_values(0) else pd.DataFrame()

            if sym_data.empty or len(sym_data) < 2:
                q = get_quote(sym)
                if q:
                    results.append(q)
                continue

            today = sym_data.iloc[-1]
            prev = sym_data.iloc[-2]
            price = _safe_float(today.get("Close"))
            prev_close = _safe_float(prev.get("Close"))
            if price == 0:
                continue
            change = price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0.0
            results.append({
                "symbol": sym,
                "name": sym,
                "price": round(price, 4),
                "change": round(change, 4),
                "change_pct": round(change_pct, 3),
                "volume": _safe_int(today.get("Volume")),
                "avg_volume": 0,
                "market_cap": None,
                "sector": "",
            })
        except Exception as e:
            logger.debug("bulk enrich %s: %s", sym, e)

    return results


# ── History / Candles ──────────────────────────────────────────────────────────

def get_history(
    symbol: str,
    period: str = "3mo",
    interval: str = "1d",
) -> List[Dict]:
    """OHLCV candles formatted for TradingView lightweight-charts."""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period=period, interval=interval, auto_adjust=True, timeout=15)
        if hist.empty:
            return []
        candles = []
        for ts, row in hist.iterrows():
            # lightweight-charts wants Unix seconds
            t_sec = int(pd.Timestamp(ts).timestamp())
            candles.append({
                "time": t_sec,
                "open": round(_safe_float(row.get("Open")), 4),
                "high": round(_safe_float(row.get("High")), 4),
                "low": round(_safe_float(row.get("Low")), 4),
                "close": round(_safe_float(row.get("Close")), 4),
                "volume": _safe_int(row.get("Volume")),
            })
        return candles
    except Exception as e:
        logger.warning("get_history %s: %s", symbol, e)
        return []


# ── News ───────────────────────────────────────────────────────────────────────

def get_news(symbol: str, limit: int = 15) -> List[Dict]:
    """Yahoo Finance news for a symbol."""
    try:
        t = yf.Ticker(symbol)
        raw = t.news or []
        items = []
        for n in raw[:limit]:
            ct = n.get("content", {})
            headline = ct.get("title") or n.get("title", "")
            if not headline:
                continue
            ts = n.get("providerPublishTime") or int(time.time())
            items.append({
                "id": n.get("id", ""),
                "headline": headline,
                "summary": ct.get("summary") or n.get("summary", ""),
                "source": (ct.get("provider") or {}).get("displayName") or n.get("publisher", ""),
                "url": (ct.get("clickThroughUrl") or {}).get("url") or n.get("link", ""),
                "timestamp": ts,
                "related_symbols": [symbol],
            })
        return items
    except Exception as e:
        logger.debug("get_news %s: %s", symbol, e)
        return []


def get_market_news(limit: int = 30) -> List[Dict]:
    """Broad market news via SPY + QQQ tickers."""
    seen: set = set()
    all_news: List[Dict] = []
    for sym in ["SPY", "QQQ", "IWM"]:
        for item in get_news(sym, limit=20):
            if item["headline"] not in seen:
                seen.add(item["headline"])
                all_news.append(item)
    all_news.sort(key=lambda x: x["timestamp"], reverse=True)
    return all_news[:limit]


# ── Momentum / Screening data ──────────────────────────────────────────────────

def get_momentum_data(symbol: str) -> Optional[Dict]:
    """
    Returns momentum scanner data for a single symbol.

    Uses fast_info for 52-week high/avg-volume (no slow t.info scrape),
    plus a 2-day history call for today's exact OHLCV.
    This keeps each call under ~0.5s vs ~15s with t.info.
    """
    try:
        t = yf.Ticker(symbol)

        # fast_info: lightweight, no full page scrape
        fi = t.fast_info
        price      = _safe_float(fi.last_price)
        prev_close = _safe_float(
            getattr(fi, "previous_close", None)
            or getattr(fi, "regular_market_previous_close", None)
        )
        avg_vol    = _safe_int(getattr(fi, "three_month_average_volume", None) or 0)

        # True 52-week high from fast_info (year_high attribute)
        high_52w = (
            _safe_float(getattr(fi, "year_high", None))
            or _safe_float(getattr(fi, "yearHigh", None))
        )

        if price == 0 or prev_close == 0:
            return None

        # 2-day daily history: today's Open + Volume (single fast request)
        hist = t.history(period="2d", interval="1d", auto_adjust=True, timeout=10)
        if hist.empty:
            return None

        today_row  = hist.iloc[-1]
        open_price = _safe_float(today_row.get("Open") or prev_close)
        current_vol = _safe_int(today_row.get("Volume") or 0)

        # Fallback: if fast_info had no year_high use history max (poor proxy but better than 0)
        if not high_52w and not hist.empty:
            high_52w = _safe_float(hist["High"].max())
        if not high_52w:
            high_52w = price

        change_pct        = (price - prev_close) / prev_close * 100
        gap_pct           = (open_price - prev_close) / prev_close * 100 if prev_close else 0
        rvol              = current_vol / avg_vol if avg_vol > 0 else 1.0
        from_52w_high_pct = (price - high_52w) / high_52w * 100 if high_52w else 0

        return {
            "symbol":             symbol,
            "company":            symbol,   # skip t.info; company name not worth 10-30s
            "price":              round(price, 4),
            "change_pct":         round(change_pct, 3),
            "relative_volume":    round(rvol, 2),
            "gap_pct":            round(gap_pct, 3),
            "from_52w_high_pct":  round(from_52w_high_pct, 3),
            "short_interest_pct": None,   # requires t.info — skipped for speed
            "float_shares":       None,
            "high_52w":           high_52w,
            "current_volume":     current_vol,
            "avg_volume":         avg_vol,
        }
    except Exception as e:
        logger.debug("momentum data %s: %s", symbol, e)
        return None


def get_analyst_info(symbol: str) -> Dict:
    """
    Pull analyst consensus data from yfinance — no API key required.
    Used by the Equity Quick Analysis route as a fast, accurate alternative
    to the LLM hedge-fund workflow when FINANCIAL_DATASETS_API_KEY is absent.

    Returns:
      recommendations     — recent broker upgrades/downgrades
      price_targets       — analyst price target stats
      earnings_estimate   — EPS estimate for next quarter
      institutional       — top institutional holders
      insiders            — recent insider transactions
      info_summary        — key company stats (sector, P/E, 52w range, etc.)
    """
    result: Dict = {
        "symbol": symbol,
        "recommendations": [],
        "price_targets": {},
        "earnings_estimate": {},
        "institutional": [],
        "insiders": [],
        "info_summary": {},
    }
    try:
        t = yf.Ticker(symbol)

        # Analyst recommendations (buy/sell/hold trend)
        try:
            rec = t.recommendations
            if rec is not None and not rec.empty:
                # Last 5 broker actions
                result["recommendations"] = (
                    rec.sort_index(ascending=False)
                    .head(10)
                    .reset_index()
                    .rename(columns={"index": "date"})
                    .to_dict("records")
                )
        except Exception:
            pass

        # Analyst price targets
        try:
            pt = t.analyst_price_targets
            if pt:
                result["price_targets"] = {
                    "current":  _safe_float(pt.get("current")),
                    "low":      _safe_float(pt.get("low")),
                    "high":     _safe_float(pt.get("high")),
                    "mean":     _safe_float(pt.get("mean")),
                    "median":   _safe_float(pt.get("median")),
                }
        except Exception:
            pass

        # Earnings estimate (next quarter EPS)
        try:
            ee = t.earnings_estimate
            if ee is not None and not ee.empty:
                row = ee.iloc[0] if len(ee) > 0 else None
                if row is not None:
                    result["earnings_estimate"] = {
                        "period":    str(ee.index[0]) if len(ee.index) > 0 else "",
                        "avg_est":   _safe_float(row.get("avg")),
                        "low_est":   _safe_float(row.get("low")),
                        "high_est":  _safe_float(row.get("high")),
                        "num_analysts": _safe_int(row.get("numberOfAnalysts")),
                    }
        except Exception:
            pass

        # Top institutional holders
        try:
            ih = t.institutional_holders
            if ih is not None and not ih.empty:
                result["institutional"] = (
                    ih.head(5)
                    .to_dict("records")
                )
        except Exception:
            pass

        # Recent insider transactions
        try:
            ins = t.insider_transactions
            if ins is not None and not ins.empty:
                result["insiders"] = (
                    ins.head(8)
                    .to_dict("records")
                )
        except Exception:
            pass

        # Key company stats (info — slow but only called once per snapshot)
        try:
            info = t.info or {}
            result["info_summary"] = {
                "name":          info.get("shortName") or symbol,
                "sector":        info.get("sector") or "",
                "industry":      info.get("industry") or "",
                "pe_ratio":      _safe_float(info.get("trailingPE")),
                "forward_pe":    _safe_float(info.get("forwardPE")),
                "ps_ratio":      _safe_float(info.get("priceToSalesTrailing12Months")),
                "pb_ratio":      _safe_float(info.get("priceToBook")),
                "market_cap":    _safe_float(info.get("marketCap")),
                "52w_high":      _safe_float(info.get("fiftyTwoWeekHigh")),
                "52w_low":       _safe_float(info.get("fiftyTwoWeekLow")),
                "beta":          _safe_float(info.get("beta")),
                "short_pct":     _safe_float(info.get("shortPercentOfFloat")) * 100,
                "dividend_yield": _safe_float(info.get("dividendYield")),
                "recommendation": info.get("recommendationKey") or "",
                "target_mean":   _safe_float(info.get("targetMeanPrice")),
                "description":   (info.get("longBusinessSummary") or "")[:400],
            }
        except Exception:
            pass

    except Exception as e:
        logger.warning("get_analyst_info %s: %s", symbol, e)

    return result


def get_etf_holdings_tickers(etf_symbol: str, limit: int = 50) -> List[str]:
    """Best-effort: get top holdings tickers from an ETF via yfinance."""
    try:
        t = yf.Ticker(etf_symbol)
        holdings = t.fund_top_holdings
        if holdings is not None and not holdings.empty:
            col = [c for c in holdings.columns if "symbol" in c.lower() or "ticker" in c.lower()]
            if col:
                return holdings[col[0]].dropna().tolist()[:limit]
    except Exception:
        pass
    return []
