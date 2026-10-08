"""
Trading rules and risk limits — the single source of truth.

All values are frozen dataclasses. Environment variables may TIGHTEN a limit
but can never loosen it past the hard ceilings defined here: `load_*()` clamps
every override toward the safe side. No agent, API route or LLM output can
mutate these objects at runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional, Tuple

UNDERLYING = "QQQ"
CONTRACT_MULTIPLIER = 100


# ── Hard ceilings (user-specified rules; never loosened by code) ──────────────

HARD_MAX_LOSS_USD = 50.0            # theoretical max loss incl. estimated fees
HARD_MAX_OPEN_POSITIONS = 1
HARD_MIN_DTE = 14
HARD_MAX_DTE = 21
HARD_SHORT_DELTA_MIN = 0.10
HARD_SHORT_DELTA_MAX = 0.15
HARD_DAILY_LOSS_LIMIT_USD = 50.0    # one full max-loss per day halts trading
STARTING_BALANCE_USD = 250.0


@dataclass(frozen=True)
class RiskLimits:
    max_loss_usd: float = HARD_MAX_LOSS_USD
    max_open_positions: int = HARD_MAX_OPEN_POSITIONS
    min_dte: int = HARD_MIN_DTE
    max_dte: int = HARD_MAX_DTE
    short_delta_min: float = HARD_SHORT_DELTA_MIN
    short_delta_max: float = HARD_SHORT_DELTA_MAX
    daily_loss_limit_usd: float = HARD_DAILY_LOSS_LIMIT_USD
    require_positive_theta: bool = True
    allowed_strategies: Tuple[str, ...] = ("bull_put", "bear_call")
    # Gamma-to-credit ceiling. Deliberately None: the user's rules forbid
    # inventing a numeric threshold. It may only be set from a validated
    # research result (see research.py). While None, every proposal carries an
    # explicit "gamma filter unvalidated" warning.
    max_gamma_to_credit: Optional[float] = None
    # Minimum time between entry and the pre-event exit deadline. Holding a
    # position for less than this is not worth the round-trip fees.
    min_hours_to_exit_deadline: float = 6.5
    # What-if research only. Sandbox limits may be looser than the user's rules, but the
    # proposal-path RiskEngine refuses them and the validator never approves their results.
    sandbox: bool = False

    def __post_init__(self) -> None:
        if self.sandbox:
            return
        problems = []
        if self.max_loss_usd > HARD_MAX_LOSS_USD:
            problems.append("max_loss_usd")
        if self.max_open_positions > HARD_MAX_OPEN_POSITIONS:
            problems.append("max_open_positions")
        if self.min_dte < HARD_MIN_DTE or self.max_dte > HARD_MAX_DTE:
            problems.append("dte window")
        if self.short_delta_min < HARD_SHORT_DELTA_MIN or self.short_delta_max > HARD_SHORT_DELTA_MAX:
            problems.append("short delta band")
        if self.daily_loss_limit_usd > HARD_DAILY_LOSS_LIMIT_USD:
            problems.append("daily_loss_limit_usd")
        if not self.require_positive_theta:
            problems.append("require_positive_theta")
        if set(self.allowed_strategies) - {"bull_put", "bear_call"}:
            problems.append("allowed_strategies")
        if problems:
            raise ValueError(f"RiskLimits looser than the user's rules: {problems} "
                             "(only sandbox=True research limits may be looser)")

    @classmethod
    def what_if(cls, **overrides) -> "RiskLimits":
        """Looser hypothetical limits for research backtests ONLY."""
        return cls(**overrides, sandbox=True)

    def version(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


@dataclass(frozen=True)
class ExitRules:
    profit_target_pct: float = 0.50       # close at 50% of max profit
    delta_warn: float = 0.30              # flag exit when |short delta| ≥ 0.30
    delta_urgent: float = 0.35            # urgent flag at ≥ 0.35
    # Pre-event exit policy (see macro_calendar.exit_deadline_for):
    #   events before the open (CPI/NFP at 08:30 ET) → exit by prior session close − buffer
    #   intraday events (FOMC 14:00 ET)              → exit by event time − buffer
    pre_event_buffer_minutes: int = 30
    expiration_warning_dte: int = 2       # flag pin/assignment risk this close to expiry


@dataclass(frozen=True)
class FeeModel:
    """Estimated per-contract costs. Broker-specific — set via env to match yours."""
    commission_per_contract: float = 0.65
    regulatory_per_contract: float = 0.05   # ORF/OCC/FINRA TAF estimate (conservative)
    assignment_fee: float = 0.0

    def per_contract(self) -> float:
        return self.commission_per_contract + self.regulatory_per_contract

    def round_trip(self, legs: int, contracts: int) -> float:
        """Open + close fees for every leg — the conservative max-loss assumption."""
        return round(2 * legs * contracts * self.per_contract(), 2)


@dataclass(frozen=True)
class DataQualityRules:
    max_quote_age_seconds: int = 30 * 60   # yfinance chains are ~15 min delayed
    max_bar_age_days: int = 4              # covers weekends + one holiday
    min_history_bars: int = 200            # need a full 200-day MA
    max_spot_vs_close_gap_pct: float = 0.08
    min_open_interest: int = 1             # data-sanity only; liquidity is ranked, not invented
    require_two_sided_quote: bool = True


@dataclass(frozen=True)
class MarketAssumptions:
    """Inputs for model-derived Greeks. Labelled as assumptions everywhere they are used."""
    risk_free_rate: float = 0.04
    dividend_yield: float = 0.006


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "")) if os.getenv(name) else default
    except ValueError:
        return default


def load_risk_limits() -> RiskLimits:
    """Env overrides may only tighten. Anything looser is clamped to the ceiling."""
    max_loss = min(_env_float("QQQ_MAX_LOSS_USD", HARD_MAX_LOSS_USD), HARD_MAX_LOSS_USD)
    daily = min(_env_float("QQQ_DAILY_LOSS_LIMIT_USD", HARD_DAILY_LOSS_LIMIT_USD),
                HARD_DAILY_LOSS_LIMIT_USD)
    dmin = max(_env_float("QQQ_SHORT_DELTA_MIN", HARD_SHORT_DELTA_MIN), HARD_SHORT_DELTA_MIN)
    dmax = min(_env_float("QQQ_SHORT_DELTA_MAX", HARD_SHORT_DELTA_MAX), HARD_SHORT_DELTA_MAX)
    if dmin > dmax:
        dmin, dmax = HARD_SHORT_DELTA_MIN, HARD_SHORT_DELTA_MAX
    gamma_env = os.getenv("QQQ_MAX_GAMMA_TO_CREDIT")
    gamma = None
    if gamma_env:
        try:
            gamma = float(gamma_env)
        except ValueError:
            gamma = None
    return RiskLimits(
        max_loss_usd=max(0.0, max_loss),
        daily_loss_limit_usd=max(0.0, daily),
        short_delta_min=dmin,
        short_delta_max=dmax,
        max_gamma_to_credit=gamma,
    )


def load_fee_model() -> FeeModel:
    # Fees may be raised (more conservative) but not set below zero.
    return FeeModel(
        commission_per_contract=max(0.0, _env_float("QQQ_COMMISSION_PER_CONTRACT", 0.65)),
        regulatory_per_contract=max(0.0, _env_float("QQQ_REGULATORY_FEE_PER_CONTRACT", 0.05)),
        assignment_fee=max(0.0, _env_float("QQQ_ASSIGNMENT_FEE", 0.0)),
    )


def load_market_assumptions() -> MarketAssumptions:
    return MarketAssumptions(
        risk_free_rate=_env_float("QQQ_RISK_FREE_RATE", 0.04),
        dividend_yield=_env_float("QQQ_DIVIDEND_YIELD", 0.006),
    )


# ── Environment separation ────────────────────────────────────────────────────

ALLOWED_ENVS = ("research", "paper")


class LiveExecutionNotAuthorized(RuntimeError):
    pass


def load_env() -> str:
    env = os.getenv("QQQ_ENV", "paper").strip().lower()
    if env == "live":
        raise LiveExecutionNotAuthorized(
            "QQQ_ENV=live is not supported. No live-order code path exists; "
            "live execution requires a separately reviewed and authorized implementation."
        )
    if env not in ALLOWED_ENVS:
        raise ValueError(f"QQQ_ENV must be one of {ALLOWED_ENVS}, got {env!r}")
    return env
