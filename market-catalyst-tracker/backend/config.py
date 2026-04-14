"""
Central configuration. Keys are optional — the app degrades gracefully:
  - No FINNHUB_API_KEY  → falls back to Yahoo Finance news only
  - No FRED_API_KEY     → macro economic overlays are skipped
"""
import os
from dotenv import load_dotenv

load_dotenv()

FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")
FRED_API_KEY: str = os.getenv("FRED_API_KEY", "")
# Nasdaq Data Link (formerly Quandl) — enables FINRA short interest, EOD history, FRED macro
NASDAQ_DATA_LINK_API_KEY: str = os.getenv("NASDAQ_DATA_LINK_API_KEY", "")

# ── Polymarket CLOB live trading ───────────────────────────────────────────────
# Required for live order execution (live_mode=True in TradingEngine).
# Without these the engine runs in paper mode silently.
#
# POLY_PRIVATE_KEY : Polygon wallet private key (0x…)
# POLY_FUNDER      : Polymarket-linked proxy wallet / funder address (0x…)
#                    (shown in Polymarket UI under "Wallet")
# POLY_HOST        : CLOB endpoint — override only for testnet/staging
POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
POLY_FUNDER:      str = os.getenv("POLY_FUNDER", "")
POLY_HOST:        str = os.getenv("POLY_HOST", "https://clob.polymarket.com")

# ── Market universe ────────────────────────────────────────────────────────────
INDICES = ["SPY", "QQQ", "IWM", "DIA"]
SECTORS = ["XLK", "XLV", "XLE", "XLF", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLB", "XLC"]

# Biotech universe — clinical-stage & commercial biotechs most exposed to FDA events
BIOTECH_TICKERS = [
    # Large-cap / mid-cap commercial
    "MRNA", "BNTX", "VRTX", "REGN", "BIIB", "GILD", "AMGN", "ILMN",
    "BMRN", "ALNY", "EXAS", "INCY", "IONS", "NBIX", "SAGE", "ACAD",
    # Clinical-stage (Phase 2/3 heavy)
    "MDGL", "KRYS", "RARE", "FOLD", "ARWR", "BEAM", "EDIT", "NTLA",
    "CRSP", "BLUE", "FATE", "KYMR", "GRPH", "RCKT", "PRAX", "VRNA",
    "HOOK", "IMVT", "DNLI", "NVAX", "NKTR", "ALKS", "HALO", "MGNX",
    "IMRS", "RVNC", "AVXL", "FULC", "ITOS", "IMCR", "RCUS", "ARVN",
    "PRTA", "XNCR", "ZNTL", "AGIO", "CCCC", "IOVA", "HRTX",
    # Oncology focus
    "AGEN", "KPTI", "ADMA", "SIGA", "PNTM", "CHRS",
]

# Broader momentum scanner universe (volatile names across sectors)
MOMENTUM_UNIVERSE = [
    # High-beta tech / growth
    "NVDA", "AMD", "SMCI", "MSTR", "TSLA", "PLTR", "SOFI", "HOOD",
    "UPST", "AFRM", "RBLX", "SNAP", "RIVN", "LCID", "JOBY", "COIN",
    # Clean energy / EV charging
    "PLUG", "FCEL", "BLNK", "CHPT", "EVGO", "BE", "RUN", "NOVA",
    # Speculative small cap
    "SPCE", "SNDL", "VINC",
    # Precious metals miners
    "GOLD", "KGC", "HL", "CDE", "PAAS", "AG",
] + BIOTECH_TICKERS

# ── Public (no-key) data endpoints ────────────────────────────────────────────
FDA_API_BASE = "https://api.fda.gov/drug"
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
CLINICAL_TRIALS_API = "https://clinicaltrials.gov/api/v2/studies"

# ── Request settings ──────────────────────────────────────────────────────────
HTTP_TIMEOUT = 20          # seconds
CACHE_TTL_SECONDS = 300    # 5-minute server-side cache for heavy scans
