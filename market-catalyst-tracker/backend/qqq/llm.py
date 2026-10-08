"""
Optional LLM drafting helper. NEVER used by the risk engine, the paper broker,
or any approval path. Output is stored verbatim and labelled AI-drafted.
Returns None (and the system carries on) when no key is configured or a call fails.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SYSTEM = ("You are a skeptical quantitative research assistant. You draft hypotheses and "
          "summaries only. Never recommend loosening risk limits, never claim profitability "
          "without out-of-sample evidence, and always state how a claim could be falsified.")


def draft(prompt: str, max_tokens: int = 600) -> Optional[str]:
    provider = os.getenv("QQQ_LLM_PROVIDER", "").lower()
    try:
        if provider in ("", "anthropic") and os.getenv("ANTHROPIC_API_KEY"):
            r = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": os.getenv("QQQ_LLM_MODEL", "claude-sonnet-5-5"), "max_tokens": max_tokens,
                      "system": SYSTEM, "messages": [{"role": "user", "content": prompt}]},
                timeout=60)
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        if provider in ("", "openai") and os.getenv("OPENAI_API_KEY"):
            r = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json={"model": os.getenv("QQQ_LLM_MODEL", "gpt-4o-mini"), "max_tokens": max_tokens,
                      "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]},
                timeout=60)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("LLM draft failed (continuing without it): %s", exc)
    return None
