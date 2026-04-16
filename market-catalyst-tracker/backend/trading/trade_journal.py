"""
Trade Journal — SQLite-backed trade recording and post-trade evaluation
========================================================================
Records every trade with the signals/conditions that triggered it.
After exit, evaluates which signals correctly predicted the outcome
so the strategy can be tuned over time.

Database: market-catalyst-tracker/backend/trade_journal.db

Tables:
  trades          — one row per trade (entry + exit when closed)
  signal_accuracy — rolling win-rate per signal type (learning loop)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent / "trade_journal.db"

# ── Schema ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT    NOT NULL,
    account_id          TEXT,
    ibkr_order_id       TEXT,

    -- Entry
    entry_time          REAL,   -- Unix timestamp
    entry_price         REAL,
    shares              REAL,
    position_value      REAL,

    -- Signals captured at entry
    momentum_score      REAL,
    rvol                REAL,
    gap_pct             REAL,
    change_pct          REAL,
    from_52w_high_pct   REAL,
    momentum_signals    TEXT,   -- JSON array e.g. ["BREAKOUT","HIGH_VOL"]
    sentiment           TEXT,   -- "bullish" / "bearish" / "neutral"
    buzz_score          REAL,
    bullish_pct         REAL,
    news_driver         TEXT,
    entry_reasons       TEXT,   -- JSON array of reasons entry was triggered
    confidence          REAL,
    strategy_version    TEXT,

    -- Exit (filled in after close)
    exit_time           REAL,
    exit_price          REAL,
    exit_reason         TEXT,   -- STOP_LOSS / TAKE_PROFIT / TRAILING / TIME / SIGNAL_REVERSAL / MANUAL
    exit_ibkr_order_id  TEXT,

    -- P&L
    gross_pnl           REAL,
    fees                REAL    DEFAULT 0,
    net_pnl             REAL,
    pnl_pct             REAL,

    -- Post-trade evaluation
    was_profitable      INTEGER,   -- 1 / 0 / NULL (open)
    max_gain_pct        REAL,      -- max unrealized gain while held
    max_loss_pct        REAL,      -- max unrealized loss while held
    hold_hours          REAL,

    -- Metadata
    notes               TEXT,
    created_at          REAL    DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS signal_accuracy (
    signal_name         TEXT    PRIMARY KEY,
    correct             INTEGER DEFAULT 0,  -- trades where signal fired and trade was profitable
    total               INTEGER DEFAULT 0,  -- trades where this signal fired at entry
    win_rate            REAL    DEFAULT 0,
    avg_pnl_pct         REAL    DEFAULT 0,
    last_updated        REAL
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date                TEXT    PRIMARY KEY,  -- YYYY-MM-DD
    trades_opened       INTEGER DEFAULT 0,
    trades_closed       INTEGER DEFAULT 0,
    realized_pnl        REAL    DEFAULT 0,
    win_count           INTEGER DEFAULT 0,
    loss_count          INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol   ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_open     ON trades (exit_time) WHERE exit_time IS NULL;
CREATE INDEX IF NOT EXISTS idx_trades_entry    ON trades (entry_time);
"""


# ── Database connection ────────────────────────────────────────────────────────

@contextmanager
def _db():
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        yield conn
    finally:
        conn.close()


def _row_to_dict(row) -> Dict:
    if row is None:
        return {}
    d = dict(row)
    # Deserialise JSON fields
    for f in ("momentum_signals", "entry_reasons"):
        if isinstance(d.get(f), str):
            try:
                d[f] = json.loads(d[f])
            except Exception:
                pass
    return d


# ── Trade recording ────────────────────────────────────────────────────────────

def record_entry(
    symbol:            str,
    entry_price:       float,
    shares:            float,
    signal:            "CompositeSignal",  # from strategy_engine
    account_id:        str = "",
    ibkr_order_id:     str = "",
) -> int:
    """
    Record a new trade entry. Returns the trade ID.
    """
    from trading.strategy_engine import STRATEGY_VERSION

    position_value = round(entry_price * shares, 2)

    with _db() as conn:
        cur = conn.execute("""
            INSERT INTO trades (
                symbol, account_id, ibkr_order_id,
                entry_time, entry_price, shares, position_value,
                momentum_score, rvol, gap_pct, change_pct, from_52w_high_pct,
                momentum_signals, sentiment, buzz_score, bullish_pct,
                news_driver, entry_reasons, confidence, strategy_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            symbol.upper(), account_id, ibkr_order_id,
            time.time(), entry_price, shares, position_value,
            signal.momentum_score, signal.rvol,
            signal.gap_pct, signal.change_pct, signal.from_52w_high_pct,
            json.dumps(signal.momentum_signals), signal.sentiment,
            signal.buzz_score, signal.bullish_pct,
            signal.news_driver, json.dumps(signal.reasons),
            signal.confidence, STRATEGY_VERSION,
        ))
        trade_id = cur.lastrowid
        conn.commit()

    logger.info("Journal: entry recorded trade_id=%d %s %.2f shares @ $%.4f",
                trade_id, symbol, shares, entry_price)
    return trade_id


def record_exit(
    trade_id:          int,
    exit_price:        float,
    exit_reason:       str,
    fees:              float = 0.0,
    exit_ibkr_order_id: str = "",
    notes:             str = "",
) -> Dict:
    """
    Record trade exit, compute P&L, update signal_accuracy table.
    Returns the completed trade dict.
    """
    with _db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if not row:
            return {}

        trade = _row_to_dict(row)
        entry_price = trade["entry_price"]
        shares      = trade["shares"]
        cost_basis  = trade["position_value"]

        gross_pnl  = round((exit_price - entry_price) * shares, 4)
        net_pnl    = round(gross_pnl - fees, 4)
        pnl_pct    = round(net_pnl / cost_basis * 100, 3) if cost_basis else 0
        profitable = 1 if net_pnl > 0 else 0
        hold_hours = round((time.time() - (trade["entry_time"] or 0)) / 3600, 2)

        conn.execute("""
            UPDATE trades SET
                exit_time=?, exit_price=?, exit_reason=?, exit_ibkr_order_id=?,
                gross_pnl=?, fees=?, net_pnl=?, pnl_pct=?,
                was_profitable=?, hold_hours=?, notes=?
            WHERE id=?
        """, (
            time.time(), exit_price, exit_reason, exit_ibkr_order_id,
            gross_pnl, fees, net_pnl, pnl_pct,
            profitable, hold_hours, notes,
            trade_id,
        ))

        conn.commit()

    # Update signal accuracy learning table
    _update_signal_accuracy(trade_id, profitable, pnl_pct)

    logger.info("Journal: exit recorded trade_id=%d %s @ $%.4f PnL $%.2f (%.2f%%)",
                trade_id, trade["symbol"], exit_price, net_pnl, pnl_pct)
    return get_trade(trade_id)


def update_peak_prices(trade_id: int, current_price: float) -> None:
    """Update max_gain_pct / max_loss_pct for open position tracking."""
    with _db() as conn:
        row = conn.execute(
            "SELECT entry_price, shares, max_gain_pct, max_loss_pct FROM trades WHERE id=? AND exit_time IS NULL",
            (trade_id,)
        ).fetchone()
        if not row:
            return

        ep = row["entry_price"]
        pct = (current_price - ep) / ep * 100 if ep else 0
        new_max_gain = max(row["max_gain_pct"] or 0, pct)
        new_max_loss = min(row["max_loss_pct"] or 0, pct)

        conn.execute(
            "UPDATE trades SET max_gain_pct=?, max_loss_pct=? WHERE id=?",
            (new_max_gain, new_max_loss, trade_id)
        )
        conn.commit()


# ── Signal accuracy (learning loop) ───────────────────────────────────────────

def _update_signal_accuracy(trade_id: int, profitable: int, pnl_pct: float) -> None:
    """Update win-rate stats for every signal that fired at this trade's entry."""
    with _db() as conn:
        row = conn.execute(
            "SELECT momentum_signals, sentiment, news_driver FROM trades WHERE id=?",
            (trade_id,)
        ).fetchone()
        if not row:
            return

        signals_to_update = []

        # Momentum signals
        try:
            msigs = json.loads(row["momentum_signals"] or "[]")
            signals_to_update.extend(msigs)
        except Exception:
            pass

        # Sentiment as a signal
        if row["sentiment"] in ("bullish", "bearish"):
            signals_to_update.append(f"SENTIMENT_{row['sentiment'].upper()}")

        # News driver
        if row["news_driver"] and row["news_driver"] != "other":
            signals_to_update.append(f"NEWS_{row['news_driver'].upper()}")

        for sig_name in set(signals_to_update):
            existing = conn.execute(
                "SELECT correct, total, avg_pnl_pct FROM signal_accuracy WHERE signal_name=?",
                (sig_name,)
            ).fetchone()

            if existing:
                new_correct = existing["correct"] + (1 if profitable else 0)
                new_total   = existing["total"] + 1
                # Rolling average P&L
                new_avg     = round(
                    (existing["avg_pnl_pct"] * existing["total"] + pnl_pct) / new_total, 3
                )
                win_rate    = round(new_correct / new_total, 3)
                conn.execute("""
                    UPDATE signal_accuracy SET
                        correct=?, total=?, win_rate=?, avg_pnl_pct=?, last_updated=?
                    WHERE signal_name=?
                """, (new_correct, new_total, win_rate, new_avg, time.time(), sig_name))
            else:
                conn.execute("""
                    INSERT INTO signal_accuracy (signal_name, correct, total, win_rate, avg_pnl_pct, last_updated)
                    VALUES (?,?,?,?,?,?)
                """, (sig_name, 1 if profitable else 0, 1,
                      1.0 if profitable else 0.0, pnl_pct, time.time()))

        conn.commit()


# ── Queries ────────────────────────────────────────────────────────────────────

def get_trade(trade_id: int) -> Dict:
    with _db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return _row_to_dict(row)


def get_open_trades(account_id: str = "") -> List[Dict]:
    """All trades with no exit recorded."""
    with _db() as conn:
        if account_id:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exit_time IS NULL AND account_id=? ORDER BY entry_time DESC",
                (account_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exit_time IS NULL ORDER BY entry_time DESC"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_trade_history(
    limit: int = 100,
    symbol: Optional[str] = None,
    account_id: str = "",
    closed_only: bool = True,
) -> List[Dict]:
    """Paginated trade history."""
    clauses = []
    params  = []
    if closed_only:
        clauses.append("exit_time IS NOT NULL")
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY COALESCE(exit_time, entry_time) DESC LIMIT ?",
            params
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_performance_stats(account_id: str = "") -> Dict:
    """
    Overall performance statistics for the learning dashboard.
    Returns win rate, avg P&L, best/worst trade, signal accuracy breakdown.
    """
    with _db() as conn:
        where = "WHERE exit_time IS NOT NULL AND account_id=?" if account_id else "WHERE exit_time IS NOT NULL"
        params = (account_id,) if account_id else ()

        agg = conn.execute(f"""
            SELECT
                COUNT(*)                        AS total_trades,
                SUM(CASE WHEN was_profitable=1 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN was_profitable=0 THEN 1 ELSE 0 END) AS losses,
                SUM(net_pnl)                    AS total_pnl,
                AVG(pnl_pct)                    AS avg_pnl_pct,
                MAX(pnl_pct)                    AS best_trade_pct,
                MIN(pnl_pct)                    AS worst_trade_pct,
                AVG(hold_hours)                 AS avg_hold_hours,
                SUM(CASE WHEN pnl_pct > 0 THEN pnl_pct ELSE 0 END) / NULLIF(COUNT(*),0) AS avg_win_pct,
                SUM(CASE WHEN pnl_pct < 0 THEN ABS(pnl_pct) ELSE 0 END) / NULLIF(SUM(CASE WHEN was_profitable=0 THEN 1 ELSE 0 END),0) AS avg_loss_pct
            FROM trades {where}
        """, params).fetchone()

        open_count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE exit_time IS NULL"
            + (" AND account_id=?" if account_id else ""),
            (account_id,) if account_id else ()
        ).fetchone()[0]

        # Signal accuracy table
        accuracy = conn.execute("""
            SELECT signal_name, correct, total, win_rate, avg_pnl_pct
            FROM signal_accuracy
            ORDER BY total DESC
            LIMIT 20
        """).fetchall()

        # Exit reason breakdown
        exits = conn.execute(f"""
            SELECT exit_reason, COUNT(*) as count, AVG(pnl_pct) as avg_pnl
            FROM trades {where} AND exit_reason IS NOT NULL
            GROUP BY exit_reason
            ORDER BY count DESC
        """, params).fetchall()

    d = dict(agg) if agg else {}
    total = d.get("total_trades") or 0
    wins  = d.get("wins") or 0

    return {
        "total_trades":    total,
        "open_positions":  open_count,
        "wins":            wins,
        "losses":          d.get("losses") or 0,
        "win_rate":        round(wins / total, 3) if total > 0 else 0,
        "total_pnl":       round(d.get("total_pnl") or 0, 2),
        "avg_pnl_pct":     round(d.get("avg_pnl_pct") or 0, 2),
        "best_trade_pct":  round(d.get("best_trade_pct") or 0, 2),
        "worst_trade_pct": round(d.get("worst_trade_pct") or 0, 2),
        "avg_hold_hours":  round(d.get("avg_hold_hours") or 0, 1),
        "signal_accuracy": [dict(r) for r in accuracy],
        "exit_breakdown":  [dict(r) for r in exits],
    }
