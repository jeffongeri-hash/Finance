"""
Command-line entry points (run from backend/):

  python -m qqq.cli status
  python -m qqq.cli cycle                       # one screening cycle → maybe a proposal
  python -m qqq.cli monitor                     # exit checks + order retries
  python -m qqq.cli macro-refresh               # pull CPI/NFP dates from FRED (FRED_API_KEY)
  python -m qqq.cli backtest --source alphavantage --start 2023-01-01 --end 2025-12-31
  python -m qqq.cli backtest --source csv --csv-dir ./my_chains --start ... --end ...
  python -m qqq.cli backtest --source synthetic --no-macro ...   # pipeline check only
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="qqq")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("cycle")
    sub.add_parser("monitor")
    sub.add_parser("macro-refresh")
    b = sub.add_parser("backtest")
    b.add_argument("--source", choices=["alphavantage", "csv", "synthetic"], required=True)
    b.add_argument("--start", required=True)
    b.add_argument("--end", required=True)
    b.add_argument("--csv-dir")
    b.add_argument("--no-macro", action="store_true",
                   help="disable the macro-event filter (report will be flagged and cannot be approved)")
    args = ap.parse_args()

    if args.cmd == "macro-refresh":
        from qqq.macro_calendar import DEFAULT_PATH, MacroCalendar, refresh_from_fred
        key = os.getenv("FRED_API_KEY")
        if not key:
            raise SystemExit("FRED_API_KEY is required")
        cal = MacroCalendar.load(DEFAULT_PATH)
        notes = refresh_from_fred(cal, key, date.today())
        cal.save(DEFAULT_PATH)
        print("\n".join(notes))
        print("FOMC dates must still be entered from federalreserve.gov (FRED does not publish them).")
        return

    from qqq.orchestrator import Orchestrator
    orch = Orchestrator()
    orch.startup()
    if args.cmd == "status":
        out = orch.status()
    elif args.cmd == "cycle":
        out = orch.run_cycle("cli")
    elif args.cmd == "monitor":
        out = orch.monitor("cli")
    else:
        out = orch.run_backtest(args.source, date.fromisoformat(args.start), date.fromisoformat(args.end),
                                csv_dir=args.csv_dir, require_macro=not args.no_macro)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
