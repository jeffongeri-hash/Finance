"""Typed, validated data models for the QQQ options pipeline (pydantic v2)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from qqq.rules import CONTRACT_MULTIPLIER, FeeModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Market data ───────────────────────────────────────────────────────────────

class Bar(BaseModel):
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @model_validator(mode="after")
    def _sane(self) -> "Bar":
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("non-positive price in bar")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close):
            raise ValueError("inconsistent OHLC")
        return self


class Trend(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    AMBIGUOUS = "AMBIGUOUS"


class UnderlyingSnapshot(BaseModel):
    symbol: str
    asof: datetime
    price: float = Field(gt=0)
    last_bar_day: date
    sma50: Optional[float] = None
    sma200: Optional[float] = None
    rsi14: Optional[float] = None
    atr14: Optional[float] = None
    hv20: Optional[float] = None
    bars: int = 0
    trend: Trend = Trend.AMBIGUOUS
    trend_reason: str = ""
    iv_context: Dict[str, Any] = Field(default_factory=dict)
    source: str = ""


GreeksSource = Literal["vendor", "model_from_quote_iv", "missing"]


class OptionQuote(BaseModel):
    underlying: str
    expiry: date
    strike: float = Field(gt=0)
    right: Literal["P", "C"]
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    last: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None    # per calendar day, per share
    vega: Optional[float] = None     # per 1 vol point, per share
    greeks_source: GreeksSource = "missing"
    quote_time: datetime
    symbol: str = ""

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2, 4)

    @property
    def has_greeks(self) -> bool:
        return None not in (self.delta, self.gamma, self.theta, self.vega)

    def osi(self) -> str:
        if self.symbol:
            return self.symbol
        return f"{self.underlying}{self.expiry:%y%m%d}{self.right}{int(round(self.strike * 1000)):08d}"


class OptionChain(BaseModel):
    underlying: str
    asof: datetime
    spot: float = Field(gt=0)
    quotes: List[OptionQuote]
    source: str = ""

    def expiries(self) -> List[date]:
        return sorted({q.expiry for q in self.quotes})

    def side(self, expiry: date, right: str) -> List[OptionQuote]:
        return sorted((q for q in self.quotes if q.expiry == expiry and q.right == right),
                      key=lambda q: q.strike)


class DataQualityReport(BaseModel):
    ok: bool
    issues: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utcnow)


# ── Spreads ───────────────────────────────────────────────────────────────────

StrategyName = Literal["bull_put", "bear_call"]


class CreditSpread(BaseModel):
    strategy: StrategyName
    short: OptionQuote
    long: OptionQuote
    contracts: int = Field(default=1, ge=1)
    limit_credit: float = Field(description="Credit the entry order will demand (per share)")
    asof: datetime

    @property
    def right(self) -> str:
        return self.short.right

    @property
    def width(self) -> float:
        return round(abs(self.short.strike - self.long.strike), 4)

    @property
    def credit_natural(self) -> float:
        return round(self.short.bid - self.long.ask, 4)

    @property
    def credit_mid(self) -> float:
        return round(self.short.mid - self.long.mid, 4)

    def dte(self, today: Optional[date] = None) -> int:
        return (self.short.expiry - (today or self.asof.date())).days

    def fees(self, fee_model: FeeModel) -> float:
        return fee_model.round_trip(legs=2, contracts=self.contracts)

    def max_profit(self, fee_model: FeeModel) -> float:
        return round(self.limit_credit * CONTRACT_MULTIPLIER * self.contracts
                     - fee_model.round_trip(legs=2, contracts=self.contracts) / 2, 2)

    def max_loss(self, fee_model: FeeModel) -> float:
        """Theoretical max loss: (width − credit) × 100 × n + open AND close fees."""
        return round((self.width - self.limit_credit) * CONTRACT_MULTIPLIER * self.contracts
                     + self.fees(fee_model), 2)

    def breakeven(self) -> float:
        if self.strategy == "bull_put":
            return round(self.short.strike - self.limit_credit, 2)
        return round(self.short.strike + self.limit_credit, 2)

    def _net(self, attr: str) -> Optional[float]:
        s, l = getattr(self.short, attr), getattr(self.long, attr)
        if s is None or l is None:
            return None
        # Position Greeks for the whole spread: short one leg, long the other.
        return round((-s + l) * CONTRACT_MULTIPLIER * self.contracts, 6)

    def net_delta(self) -> Optional[float]:
        return self._net("delta")

    def net_gamma(self) -> Optional[float]:
        return self._net("gamma")

    def net_theta(self) -> Optional[float]:
        return self._net("theta")

    def net_vega(self) -> Optional[float]:
        return self._net("vega")

    def gamma_to_credit(self) -> Optional[float]:
        g = self.net_gamma()
        credit_usd = self.limit_credit * CONTRACT_MULTIPLIER * self.contracts
        if g is None or credit_usd <= 0:
            return None
        return round(abs(g) / credit_usd, 6)

    def summary(self, fee_model: FeeModel, today: Optional[date] = None) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "expiry": self.short.expiry.isoformat(),
            "dte": self.dte(today),
            "short_strike": self.short.strike,
            "long_strike": self.long.strike,
            "short_symbol": self.short.osi(),
            "long_symbol": self.long.osi(),
            "width": self.width,
            "contracts": self.contracts,
            "credit_natural": self.credit_natural,
            "credit_mid": self.credit_mid,
            "limit_credit": self.limit_credit,
            "fees_round_trip": self.fees(fee_model),
            "max_profit": self.max_profit(fee_model),
            "max_loss": self.max_loss(fee_model),
            "breakeven": self.breakeven(),
            "short_delta": self.short.delta,
            "short_iv": self.short.iv,
            "net_delta": self.net_delta(),
            "net_gamma": self.net_gamma(),
            "net_theta": self.net_theta(),
            "net_vega": self.net_vega(),
            "gamma_to_credit": self.gamma_to_credit(),
            "greeks_source": self.short.greeks_source,
            "short_oi": self.short.open_interest,
            "long_oi": self.long.open_interest,
            "short_bid_ask": [self.short.bid, self.short.ask],
            "long_bid_ask": [self.long.bid, self.long.ask],
        }


# ── Risk ──────────────────────────────────────────────────────────────────────

class RiskCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class RiskDecision(BaseModel):
    approved: bool
    checks: List[RiskCheck]
    warnings: List[str] = Field(default_factory=list)
    limits_version: str
    evaluated_at: datetime = Field(default_factory=utcnow)

    def failed(self) -> List[RiskCheck]:
        return [c for c in self.checks if not c.passed]


# ── Macro ─────────────────────────────────────────────────────────────────────

class MacroKind(str, Enum):
    CPI = "CPI"
    FOMC = "FOMC"
    NFP = "NFP"


class MacroEvent(BaseModel):
    kind: MacroKind
    at: datetime                     # timezone-aware, America/New_York
    source: str                      # URL of the official schedule
    note: str = ""

    @field_validator("at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("macro event time must be timezone-aware")
        return v


# ── Proposals ─────────────────────────────────────────────────────────────────

ProposalStatus = Literal["pending_approval", "approved", "rejected", "expired", "submitted"]


class ExitPlan(BaseModel):
    profit_target_debit: float          # close when the debit to close ≤ this (per share)
    delta_warn: float
    delta_urgent: float
    event_exit_deadline: Optional[datetime] = None
    event_exit_reason: str = ""
    disclaimer: str = (
        "Exit triggers are proposals only and are not resting orders. Even when placed, "
        "stop and limit orders do not guarantee a fill or a fill price; gaps, halts and "
        "wide markets can produce losses up to the full max loss."
    )


class TradeProposal(BaseModel):
    id: str
    kind: Literal["entry", "exit"] = "entry"
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    status: ProposalStatus = "pending_approval"
    env: str
    strategy_version: str
    strategy_validation: str             # "validated:<report_id>" or "UNVALIDATED"
    spread: Dict[str, Any]
    spread_model: Optional[CreditSpread] = None
    thesis: List[str]
    invalidation: List[str]
    exit_plan: Optional[ExitPlan] = None
    risk: RiskDecision
    warnings: List[str] = Field(default_factory=list)
    position_id: Optional[str] = None    # for exit proposals
    exit_reasons: List[str] = Field(default_factory=list)
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    decision_note: str = ""


# ── Paper broker ──────────────────────────────────────────────────────────────

OrderStatus = Literal["working", "partially_filled", "filled", "cancelled", "rejected"]


class PaperOrder(BaseModel):
    client_order_id: str
    proposal_id: str
    intent: Literal["open", "close"]
    position_id: Optional[str] = None
    strategy: StrategyName
    short_symbol: str
    long_symbol: str
    expiry: date
    short_strike: float
    long_strike: float
    right: Literal["P", "C"]
    contracts: int = Field(ge=1)
    limit_price: float                   # credit for open, debit for close (per share)
    filled_contracts: int = 0
    avg_fill_price: Optional[float] = None
    status: OrderStatus = "working"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    note: str = ""


class PaperPosition(BaseModel):
    id: str
    strategy: StrategyName
    short_symbol: str
    long_symbol: str
    expiry: date
    short_strike: float
    long_strike: float
    right: Literal["P", "C"]
    contracts: int
    entry_credit: float
    entry_fees: float
    opened_at: datetime
    status: Literal["open", "closed"] = "open"
    exit_debit: Optional[float] = None
    exit_fees: float = 0.0
    closed_at: Optional[datetime] = None
    realized_pnl: Optional[float] = None
    exit_plan: Optional[ExitPlan] = None
    proposal_id: str = ""

    @property
    def width(self) -> float:
        return abs(self.short_strike - self.long_strike)


# ── Backtest / validation ─────────────────────────────────────────────────────

class BacktestTrade(BaseModel):
    strategy: StrategyName
    entry_day: date
    exit_day: date
    expiry: date
    short_strike: float
    long_strike: float
    contracts: int
    entry_credit: float
    exit_debit: float
    fees: float
    pnl: float
    exit_reason: str
    max_loss_at_entry: float
    gamma_to_credit: Optional[float] = None
    iv_percentile: Optional[float] = None
    short_delta: Optional[float] = None


class BacktestReport(BaseModel):
    id: str
    strategy_version: str
    proposer: str
    data_source: str
    synthetic: bool
    lookahead_guard: bool
    fill_model: str
    slippage_fraction: float
    fill_lag_days: int
    macro_filter: bool
    start: date
    end: date
    params: Dict[str, Any]
    trades: List[BacktestTrade]
    equity_curve: List[List[Any]]          # [[iso_date, equity], ...]
    days_total: int
    days_missing_data: int
    days_missing_greeks: int
    metrics: Dict[str, Any]
    created_at: datetime = Field(default_factory=utcnow)
    walk_forward: Optional[Dict[str, Any]] = None


class ValidationReport(BaseModel):
    id: str
    backtest_id: str
    strategy_version: str
    validator: str
    proposer: str
    status: Literal["APPROVED", "REJECTED"]
    reasons: List[str]
    warnings: List[str]
    metrics: Dict[str, Any]
    created_at: datetime = Field(default_factory=utcnow)
