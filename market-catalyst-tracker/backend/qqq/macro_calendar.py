"""
Macro event calendar (CPI, FOMC, Employment Situation) and pre-event exit deadlines.

Fail-closed design: event dates are NOT hard-coded. They come from
`qqq/data/macro_events.json` (each entry must cite its official source URL)
and, for CPI/NFP, can be refreshed from the FRED release calendar. Each event
kind has a `verified_through` date. If the calendar is not verified through a
spread's expiration for EVERY kind, the risk engine rejects the trade.

Official sources:
  FOMC — https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  CPI  — https://www.bls.gov/schedule/news_release/cpi.htm
  NFP  — https://www.bls.gov/schedule/news_release/empsit.htm
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from qqq.models import MacroEvent, MacroKind
from qqq.rules import ExitRules

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
DEFAULT_PATH = Path(__file__).parent / "data" / "macro_events.json"

FRED_RELEASES = {MacroKind.CPI: 10, MacroKind.NFP: 50}   # Consumer Price Index; Employment Situation
BLS_RELEASE_TIME = time(8, 30)


class MacroCalendar:
    def __init__(self, events: List[MacroEvent], verified_through: Dict[MacroKind, date],
                 holidays: Dict[date, str] | None = None,
                 early_closes: Dict[date, time] | None = None,
                 verified_from: Dict[MacroKind, date] | None = None):
        self.events = sorted(events, key=lambda e: e.at)
        self.verified_through = verified_through
        self.verified_from = verified_from or {}
        self.holidays = holidays or {}
        self.early_closes = early_closes or {}

    # ── loading ───────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> "MacroCalendar":
        p = Path(path)
        if not p.exists():
            return cls([], {})
        raw = json.loads(p.read_text())
        events = []
        for e in raw.get("events", []):
            if not e.get("source"):
                logger.warning("macro event without source ignored: %s", e)
                continue
            events.append(MacroEvent(**e))
        vt = {}
        for k, v in (raw.get("verified_through") or {}).items():
            if v:
                vt[MacroKind(k)] = date.fromisoformat(v)
        vf = {MacroKind(k): date.fromisoformat(v) for k, v in (raw.get("verified_from") or {}).items() if v}
        holidays = {date.fromisoformat(h["day"]): h.get("source", "")
                    for h in raw.get("market_holidays", []) if h.get("source")}
        early = {date.fromisoformat(h["day"]): time.fromisoformat(h["close"])
                 for h in raw.get("early_closes", []) if h.get("source")}
        return cls(events, vt, holidays, early, vf)

    def to_json(self) -> dict:
        return {
            "verified_through": {k.value: v.isoformat() for k, v in self.verified_through.items()},
            "verified_from": {k.value: v.isoformat() for k, v in self.verified_from.items()},
            "events": [json.loads(e.model_dump_json()) for e in self.events],
            "market_holidays": [{"day": d.isoformat(), "source": s} for d, s in sorted(self.holidays.items())],
            "early_closes": [{"day": d.isoformat(), "close": t.isoformat(timespec="minutes"),
                              "source": "user-maintained"} for d, t in sorted(self.early_closes.items())],
        }

    def save(self, path: Path | str = DEFAULT_PATH) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2) + "\n")

    # ── coverage ──────────────────────────────────────────────────────────────

    def coverage_gaps(self, through: date) -> List[str]:
        gaps = []
        for kind in MacroKind:
            vt = self.verified_through.get(kind)
            if vt is None:
                gaps.append(f"{kind.value}: calendar not verified")
            elif vt < through:
                gaps.append(f"{kind.value}: verified only through {vt.isoformat()}, need {through.isoformat()}")
        return gaps

    def history_gaps(self, since: date) -> List[str]:
        """For backtests: every kind must be verified complete from `since` onward."""
        gaps = []
        for kind in MacroKind:
            vf = self.verified_from.get(kind)
            if vf is None or vf > since:
                gaps.append(f"{kind.value}: historical dates not verified from {since.isoformat()}")
        return gaps

    # ── sessions ──────────────────────────────────────────────────────────────

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def previous_trading_day(self, d: date) -> date:
        cur = d - timedelta(days=1)
        for _ in range(10):
            if self.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        raise RuntimeError("no trading day found in the previous 10 days")

    def session_close(self, d: date) -> datetime:
        return datetime.combine(d, self.early_closes.get(d, SESSION_CLOSE), tzinfo=ET)

    def is_market_open(self, now: datetime) -> bool:
        local = now.astimezone(ET)
        if not self.is_trading_day(local.date()):
            return False
        return datetime.combine(local.date(), SESSION_OPEN, tzinfo=ET) <= local < self.session_close(local.date())

    # ── events ────────────────────────────────────────────────────────────────

    def next_event(self, now: datetime, kinds: Optional[List[MacroKind]] = None) -> Optional[MacroEvent]:
        for e in self.events:
            if e.at > now and (kinds is None or e.kind in kinds):
                return e
        return None

    def events_between(self, start: datetime, end: datetime) -> List[MacroEvent]:
        return [e for e in self.events if start < e.at <= end]

    def exit_deadline_for(self, event: MacroEvent, rules: ExitRules) -> datetime:
        """Latest time a position may still be open before `event`."""
        local = event.at.astimezone(ET)
        buffer = timedelta(minutes=rules.pre_event_buffer_minutes)
        open_dt = datetime.combine(local.date(), SESSION_OPEN, tzinfo=ET)
        if local <= open_dt or not self.is_trading_day(local.date()):
            # Pre-market release: last chance is the prior session's close.
            prior = self.previous_trading_day(local.date())
            return self.session_close(prior) - buffer
        return local - buffer

    def position_deadline(self, now: datetime, expiry: date, rules: ExitRules) -> Tuple[Optional[datetime], str]:
        """Pre-event exit deadline for the first event between now and expiration close.
        The returned deadline may already be in the past — callers treat that as 'exit now'."""
        for e in self.events_between(now, self.session_close(expiry)):
            return self.exit_deadline_for(e, rules), f"{e.kind.value} at {e.at.astimezone(ET):%Y-%m-%d %H:%M} ET"
        return None, ""


# ── FRED refresh (CPI + Employment Situation release dates) ────────────────────

def fetch_fred_release_dates(kind: MacroKind, api_key: str, start: date) -> List[date]:
    release_id = FRED_RELEASES[kind]
    r = httpx.get(
        "https://api.stlouisfed.org/fred/release/dates",
        params={"release_id": release_id, "api_key": api_key, "file_type": "json",
                "realtime_start": start.isoformat(), "realtime_end": "9999-12-31",
                "include_release_dates_with_no_data": "true", "sort_order": "asc", "limit": 1000},
        timeout=20,
    )
    r.raise_for_status()
    return sorted({date.fromisoformat(x["date"]) for x in r.json().get("release_dates", [])
                   if date.fromisoformat(x["date"]) >= start})


def refresh_from_fred(cal: MacroCalendar, api_key: str, today: date) -> List[str]:
    """Merge FRED-scheduled CPI/NFP dates (08:30 ET) into the calendar. Returns change notes."""
    notes = []
    for kind, rid in FRED_RELEASES.items():
        days = fetch_fred_release_dates(kind, api_key, today)
        if not days:
            notes.append(f"{kind.value}: FRED returned no future dates")
            continue
        existing = {(e.kind, e.at.date()) for e in cal.events}
        for d in days:
            if (kind, d) not in existing:
                cal.events.append(MacroEvent(
                    kind=kind, at=datetime.combine(d, BLS_RELEASE_TIME, tzinfo=ET),
                    source=f"https://api.stlouisfed.org/fred/release/dates?release_id={rid}",
                ))
        cal.verified_through[kind] = max(days)
        notes.append(f"{kind.value}: {len(days)} dates through {max(days).isoformat()}")
    cal.events.sort(key=lambda e: e.at)
    return notes
