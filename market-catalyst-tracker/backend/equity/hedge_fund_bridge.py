"""
Equity AI bridge — wraps the ai-hedge-fund LangGraph analyst workflow.

The ai-hedge-fund repo lives at AI_HEDGE_FUND_PATH (configured in config.py).
We add it to sys.path at import time so its `src.*` packages are importable
without copying any code into this project.

Public surface:
  equity_status()                    → availability dict
  list_agents()                      → list of {key, display_name, ...}
  run_analysis(tickers, analysts,    → {decisions, analyst_signals, meta}
               model_name, provider,
               start_date, end_date)
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)

# ── Path injection ─────────────────────────────────────────────────────────────

def _inject_path() -> str | None:
    """Add the ai-hedge-fund directory to sys.path. Returns path or None."""
    try:
        from config import AI_HEDGE_FUND_PATH
    except ImportError:
        return None

    if not os.path.isdir(AI_HEDGE_FUND_PATH):
        return None

    if AI_HEDGE_FUND_PATH not in sys.path:
        sys.path.insert(0, AI_HEDGE_FUND_PATH)
    return AI_HEDGE_FUND_PATH


def _set_env_keys() -> None:
    """Push our config keys into the environment so ai-hedge-fund reads them."""
    try:
        from config import (
            FINANCIAL_DATASETS_API_KEY,
            OPENAI_API_KEY,
            ANTHROPIC_API_KEY,
            GROQ_API_KEY,
            DEEPSEEK_API_KEY,
        )
        if FINANCIAL_DATASETS_API_KEY:
            os.environ.setdefault("FINANCIAL_DATASETS_API_KEY", FINANCIAL_DATASETS_API_KEY)
        if OPENAI_API_KEY:
            os.environ.setdefault("OPENAI_API_KEY", OPENAI_API_KEY)
        if ANTHROPIC_API_KEY:
            os.environ.setdefault("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)
        if GROQ_API_KEY:
            os.environ.setdefault("GROQ_API_KEY", GROQ_API_KEY)
        if DEEPSEEK_API_KEY:
            os.environ.setdefault("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)
    except ImportError:
        pass


# Inject at module load time.
_HEDGE_FUND_PATH = _inject_path()
_set_env_keys()


# ── Availability check ─────────────────────────────────────────────────────────

def _check_imports() -> tuple[bool, str]:
    """Return (ok, error_message). Lazy-checks all required packages."""
    if not _HEDGE_FUND_PATH:
        return False, "ai-hedge-fund directory not found"
    try:
        import langchain          # noqa: F401
        import langgraph          # noqa: F401
        import langchain_core     # noqa: F401
    except ImportError as e:
        return False, f"Missing dependency: {e.name} — run: pip install langchain langgraph langchain-openai"

    try:
        from src.utils.analysts import ANALYST_CONFIG  # noqa: F401
    except ImportError as e:
        return False, f"Cannot import ai-hedge-fund src: {e}"

    return True, ""


def equity_status() -> dict:
    """Return availability and configuration status of the equity AI engine."""
    try:
        from config import (
            FINANCIAL_DATASETS_API_KEY,
            OPENAI_API_KEY, ANTHROPIC_API_KEY,
            GROQ_API_KEY, DEEPSEEK_API_KEY,
            EQUITY_MODEL_NAME, EQUITY_MODEL_PROVIDER,
            AI_HEDGE_FUND_PATH,
        )
    except ImportError:
        return {"available": False, "error": "config.py missing"}

    ok, err = _check_imports()
    has_llm = any([OPENAI_API_KEY, ANTHROPIC_API_KEY, GROQ_API_KEY, DEEPSEEK_API_KEY])

    return {
        "available": ok and bool(FINANCIAL_DATASETS_API_KEY) and has_llm,
        "imports_ok": ok,
        "import_error": err or None,
        "has_financial_data_key": bool(FINANCIAL_DATASETS_API_KEY),
        "has_llm_key": has_llm,
        "model_name": EQUITY_MODEL_NAME,
        "model_provider": EQUITY_MODEL_PROVIDER,
        "hedge_fund_path": AI_HEDGE_FUND_PATH,
        "warnings": _build_warnings(ok, err, bool(FINANCIAL_DATASETS_API_KEY), has_llm),
    }


def _build_warnings(imports_ok: bool, err: str, has_data: bool, has_llm: bool) -> list[str]:
    w = []
    if not imports_ok:
        w.append(f"Python packages missing: {err}")
    if not has_data:
        w.append("FINANCIAL_DATASETS_API_KEY not set — analyst agents will fail to fetch fundamental data")
    if not has_llm:
        w.append("No LLM API key set (OPENAI_API_KEY / ANTHROPIC_API_KEY / GROQ_API_KEY / DEEPSEEK_API_KEY)")
    return w


# ── Agent catalogue ────────────────────────────────────────────────────────────

def list_agents() -> list[dict]:
    """Return all available analyst agents in display order."""
    ok, _ = _check_imports()
    if not ok:
        return []
    try:
        from src.utils.analysts import get_agents_list
        return get_agents_list()
    except Exception as e:
        logger.warning("list_agents failed: %s", e)
        return []


# ── Core analysis runner ───────────────────────────────────────────────────────

_executor = ThreadPoolExecutor(max_workers=4)


def _build_default_portfolio(tickers: list[str], cash: float = 100_000.0) -> dict:
    return {
        "cash": cash,
        "margin_requirement": 0.0,
        "margin_used": 0.0,
        "positions": {
            ticker: {
                "long": 0,
                "short": 0,
                "long_cost_basis": 0.0,
                "short_cost_basis": 0.0,
                "short_margin_used": 0.0,
            }
            for ticker in tickers
        },
        "realized_gains": {
            ticker: {"long": 0.0, "short": 0.0}
            for ticker in tickers
        },
    }


def _run_hedge_fund_sync(
    tickers: list[str],
    analysts: list[str] | None,
    model_name: str,
    model_provider: str,
    start_date: str,
    end_date: str,
    show_reasoning: bool,
) -> dict:
    """Synchronous wrapper around ai-hedge-fund's run_hedge_fund()."""
    from src.main import run_hedge_fund

    portfolio = _build_default_portfolio(tickers)
    return run_hedge_fund(
        tickers=tickers,
        start_date=start_date,
        end_date=end_date,
        portfolio=portfolio,
        show_reasoning=show_reasoning,
        selected_analysts=analysts or [],
        model_name=model_name,
        model_provider=model_provider,
    )


async def run_analysis(
    tickers: list[str],
    analysts: list[str] | None = None,
    model_name: str | None = None,
    model_provider: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    show_reasoning: bool = False,
) -> dict:
    """
    Async wrapper around the ai-hedge-fund LangGraph workflow.

    Returns a normalised dict ready for JSON serialisation:
    {
        "tickers":         [...],
        "analysts_used":   [...],
        "model":           "gpt-4o-mini",
        "provider":        "OpenAI",
        "start_date":      "2025-01-01",
        "end_date":        "2026-04-14",
        "decisions":       {ticker: {action, quantity, confidence, reasoning}},
        "analyst_signals": {analyst_key: {ticker: {signal, confidence, reasoning}}},
        "summary":         {ticker: {bullish, bearish, neutral, top_signal}},
        "elapsed_s":       12.3,
        "timestamp":       1713000000,
    }
    """
    ok, err = _check_imports()
    if not ok:
        raise RuntimeError(f"Equity AI unavailable: {err}")

    # Defaults
    try:
        from config import EQUITY_MODEL_NAME, EQUITY_MODEL_PROVIDER
    except ImportError:
        EQUITY_MODEL_NAME, EQUITY_MODEL_PROVIDER = "gpt-4o-mini", "OpenAI"

    model_name     = model_name     or EQUITY_MODEL_NAME
    model_provider = model_provider or EQUITY_MODEL_PROVIDER

    today = datetime.date.today()
    end_date   = end_date   or today.strftime("%Y-%m-%d")
    start_date = start_date or (today - relativedelta(months=3)).strftime("%Y-%m-%d")

    tickers = [t.upper() for t in tickers]

    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(
        _executor,
        _run_hedge_fund_sync,
        tickers, analysts, model_name, model_provider,
        start_date, end_date, show_reasoning,
    )
    elapsed = round(time.perf_counter() - t0, 2)

    decisions       = raw.get("decisions") or {}
    analyst_signals = raw.get("analyst_signals") or {}

    # Build per-ticker signal summary (count bull/bear/neutral)
    summary: dict[str, dict] = {}
    for ticker in tickers:
        bull = bear = neut = 0
        top: dict | None = None
        top_conf = -1.0
        for analyst_key, ticker_map in analyst_signals.items():
            sig = ticker_map.get(ticker, {})
            if not sig:
                continue
            s = (sig.get("signal") or "").lower()
            c = float(sig.get("confidence", 0) or 0)
            if s == "bullish":
                bull += 1
            elif s == "bearish":
                bear += 1
            else:
                neut += 1
            if c > top_conf:
                top_conf = c
                top = {"analyst": analyst_key, **sig}
        summary[ticker] = {
            "bullish": bull,
            "bearish": bear,
            "neutral": neut,
            "total":   bull + bear + neut,
            "top_signal": top,
        }

    # Normalise analysts_used — from keys present in analyst_signals
    analysts_used = list(analyst_signals.keys()) or analysts or []

    return {
        "tickers":         tickers,
        "analysts_used":   analysts_used,
        "model":           model_name,
        "provider":        model_provider,
        "start_date":      start_date,
        "end_date":        end_date,
        "decisions":       decisions,
        "analyst_signals": analyst_signals,
        "summary":         summary,
        "elapsed_s":       elapsed,
        "timestamp":       int(time.time()),
    }
