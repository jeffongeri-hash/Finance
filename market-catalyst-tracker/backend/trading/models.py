"""
Trading engine data models.
All trades are paper/simulation by default — no real funds until a live
API key + wallet are explicitly configured.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
import time


class MarketPhase(Enum):
    """State machine phases for a single prediction market lifecycle."""
    PENDING    = "pending"    # market slot not yet open
    ACTIVE     = "active"     # currently tradeable
    RESOLVING  = "resolving"  # past end time, awaiting resolution
    RESOLVED   = "resolved"   # final outcome known
    CANCELLED  = "cancelled"  # market voided


class OrderSide(Enum):
    YES = "yes"
    NO  = "no"


class OrderStatus(Enum):
    PENDING   = "pending"
    MATCHED   = "matched"    # off-chain order matched
    MINED     = "mined"      # submitted to blockchain
    CONFIRMED = "confirmed"  # N confirmations (ready to sell)
    CANCELLED = "cancelled"
    FILLED    = "filled"
    PARTIAL   = "partial"


class SignalType(Enum):
    """Types of edges we detect."""
    ORDER_BOOK_IMBALANCE   = "order_book_imbalance"    # bid/ask depth skew
    PRICE_DIVERGENCE       = "price_divergence"         # Binance vs Chainlink spread
    ARBITRAGE_MACRO        = "arbitrage_macro"          # fin-market vs PM probability gap
    BIOTECH_CATALYST       = "biotech_catalyst"         # FDA event + historical approval rate
    NEWS_LAG               = "news_lag"                 # market slow to reprice on news
    MOMENTUM_CORRELATION   = "momentum_correlation"     # stock squeeze → PM event
    TIME_OF_DAY            = "time_of_day"              # pattern-based window
    PENNY_HARVEST          = "penny_harvest"            # 1c dead-contract asymmetric EV farm


@dataclass
class Order:
    market_id:   str
    side:        OrderSide
    price:       float          # limit price 0.01–0.99
    shares:      float          # number of shares (= $ at price ÷ price)
    status:      OrderStatus = OrderStatus.PENDING
    filled_at:   Optional[float] = None   # price actually filled
    filled_size: float = 0.0
    slippage_bps: float = 0.0
    created_at:  float = field(default_factory=time.time)
    settled_at:  Optional[float] = None   # after on-chain confirmation
    order_id:    str = ""


@dataclass
class Position:
    market_id:   str
    side:        OrderSide
    shares:      float
    avg_price:   float
    cost_basis:  float          # total $ spent
    opened_at:   float = field(default_factory=time.time)
    settled:     bool = False   # True once on-chain confirmed (can sell)


@dataclass
class Trade:
    """A completed round-trip: buy → sell (or hold to resolution)."""
    market_id:   str
    question:    str
    side:        OrderSide
    entry_price: float
    exit_price:  Optional[float]      # None = resolved (win 1.0, lose 0.0)
    shares:      float
    gross_pnl:   float                # before fees
    fee_cost:    float
    net_pnl:     float
    pnl_pct:     float                # net_pnl / cost_basis
    duration_s:  float
    signal_type: str = ""
    resolved_outcome: Optional[str] = None
    timestamp:   float = field(default_factory=time.time)


@dataclass
class MarketContext:
    """Everything the engine knows about one prediction market slot."""
    condition_id:   str
    question:       str
    slug:           str
    yes_token_id:   str
    no_token_id:    str = ""
    phase:          MarketPhase = MarketPhase.PENDING
    end_time:       Optional[float] = None
    yes_price:      Optional[float] = None    # latest midpoint
    no_price:       Optional[float] = None
    volume:         float = 0.0
    liquidity:      float = 0.0
    order_book:     Optional[Dict] = None
    metadata:       Dict[str, Any] = field(default_factory=dict)


@dataclass
class EngineState:
    """Mutable engine state shared across the lifecycle."""
    balance:    float         # paper balance in $
    positions:  List[Position]  = field(default_factory=list)
    orders:     List[Order]     = field(default_factory=list)
    trade_log:  List[Trade]     = field(default_factory=list)
    total_pnl:  float = 0.0
    win_count:  int   = 0
    loss_count: int   = 0
    started_at: float = field(default_factory=time.time)

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total else 0.0

    @property
    def total_trades(self) -> int:
        return len(self.trade_log)

    @property
    def portfolio_value(self) -> float:
        return self.balance + sum(
            p.shares * (0.5 if not p.settled else p.avg_price)
            for p in self.positions
        )
