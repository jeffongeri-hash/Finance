"""Pydantic response models — all API endpoints return these shapes."""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, List


class StockQuote(BaseModel):
    symbol: str
    name: str = ""
    price: float
    change: float
    change_pct: float
    volume: int = 0
    avg_volume: int = 0
    market_cap: Optional[float] = None
    sector: str = ""


class Candle(BaseModel):
    time: int          # Unix timestamp (seconds)
    open: float
    high: float
    low: float
    close: float
    volume: int


class NewsItem(BaseModel):
    id: str = ""
    headline: str
    summary: str = ""
    source: str = ""
    url: str = ""
    timestamp: int     # Unix timestamp
    sentiment_score: float = 0.0   # -1 (bearish) → +1 (bullish)
    driver_category: str = "other" # fed_policy | earnings | inflation | geopolitical | ai_tech …
    related_symbols: List[str] = Field(default_factory=list)


class MarketOverview(BaseModel):
    indices: List[StockQuote]
    sectors: List[StockQuote]
    top_gainers: List[StockQuote]
    top_losers: List[StockQuote]
    timestamp: int


class NewsCorrelation(BaseModel):
    """Explains why the market is moving right now."""
    market_symbol: str
    market_change_pct: float
    primary_driver: str
    driver_label: str
    confidence: float          # 0–1
    summary: str
    supporting_news: List[NewsItem]
    timestamp: int


class CatalystEvent(BaseModel):
    symbol: str
    company: str
    event_type: str            # FDA_PDUFA | FDA_ADCOM | PHASE3_RESULT | NDA_SUBMISSION | SEC_8K
    event_date: Optional[str] = None
    days_until: Optional[int] = None
    description: str
    priority: str = "MEDIUM"  # HIGH | MEDIUM | LOW
    market_cap: Optional[float] = None
    price: Optional[float] = None
    price_change_pct: Optional[float] = None
    short_interest_pct: Optional[float] = None
    source_url: str = ""


class MomentumCandidate(BaseModel):
    symbol: str
    company: str
    price: float
    change_pct: float
    relative_volume: float       # current day vol / 10-day avg vol
    gap_pct: Optional[float] = None
    from_52w_high_pct: float     # 0 = AT high, negative = below, positive = above
    short_interest_pct: Optional[float] = None
    float_shares: Optional[float] = None
    signals: List[str] = Field(default_factory=list)  # BREAKOUT | GAP_UP | SQUEEZE | HIGH_VOL
    score: float = 0.0           # 0–100 composite score


class ScanResult(BaseModel):
    candidates: List[MomentumCandidate]
    scanned: int
    timestamp: int


class PredictionMarket(BaseModel):
    """A single Polymarket binary market."""
    condition_id: str
    slug: str = ""
    question: str
    description: str = ""
    outcomes: List[str] = Field(default_factory=lambda: ["Yes", "No"])
    yes_price: Optional[float] = None   # 0.0–1.0 — the crowd-implied probability
    no_price:  Optional[float] = None
    yes_token_id: Optional[str] = None
    volume: float = 0.0                 # total $ traded
    liquidity: float = 0.0             # current $ in order book
    active: bool = True
    closed: bool = False
    end_date: str = ""
    tags: List[str] = Field(default_factory=list)
    url: str = ""
    category: str = "other"            # biotech_fda | macro | geopolitical | crypto | other
    price_source: str = "gamma_cached" # gamma_cached | clob_live


class EnrichedCatalyst(BaseModel):
    """CatalystEvent extended with an optional matched Polymarket market."""
    symbol: str
    company: str
    event_type: str
    event_date: Optional[str] = None
    days_until: Optional[int] = None
    description: str
    priority: str = "MEDIUM"
    market_cap: Optional[float] = None
    price: Optional[float] = None
    price_change_pct: Optional[float] = None
    short_interest_pct: Optional[float] = None
    source_url: str = ""
    # Polymarket attachment — None if no matching market found
    prediction_market: Optional[PredictionMarket] = None


class PredictionSnapshot(BaseModel):
    """Full snapshot of prediction market data by category."""
    top: List[PredictionMarket]
    biotech_fda: List[PredictionMarket]
    macro: List[PredictionMarket]
    geopolitical: List[PredictionMarket]
    timestamp: int
