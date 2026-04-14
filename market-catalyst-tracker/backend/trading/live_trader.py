"""
Live Order Execution Layer — Polymarket CLOB
=============================================
Executes real orders on Polymarket via py-clob-client.
Mirrors PaperTrader's interface exactly so TradingEngine can use either.

Prerequisites:
  pip install py-clob-client
  POLY_PRIVATE_KEY=0x...  in .env   (Polygon wallet private key)
  POLY_FUNDER=0x...       in .env   (Polymarket-linked wallet / funder address)
  POLY_HOST=https://clob.polymarket.com  (default, usually not overridden)

Risk warning:
  This class sends REAL GTC limit orders to the Polymarket CLOB on Polygon mainnet.
  Real USDC is spent. Verify credentials, position sizing, and risk controls before
  setting live_mode=True in the engine.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional, List, Dict, Tuple, Any

from trading.models import (
    Order, OrderSide, OrderStatus, Position, Trade,
    MarketContext, EngineState,
)

logger = logging.getLogger(__name__)

POLY_HOST       = "https://clob.polymarket.com"
POLY_CHAIN_ID   = 137   # Polygon mainnet
POLY_SIG_TYPE   = 1     # EIP-712 typed-data signing
POLY_FEE_BPS    = 20    # 20 bps Polymarket taker fee


# ── py-clob-client lazy loader ────────────────────────────────────────────────

def _clob_imports():
    """
    Lazy-import py-clob-client so the rest of the app boots without it.
    Raises ImportError with install hint if missing.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.constants import BUY, SELL
        return ClobClient, OrderArgs, OrderType, BUY, SELL
    except ImportError:
        raise ImportError(
            "py-clob-client is not installed. "
            "Run: pip install py-clob-client\n"
            "Then set POLY_PRIVATE_KEY + POLY_FUNDER in your .env file."
        )


def _calc_fee(price: float, shares: float) -> float:
    """Polymarket fee: fee_bps/10000 × min(price, 1-price) × shares."""
    return (POLY_FEE_BPS / 10_000) * min(price, 1 - price) * shares


# ── LiveTrader ────────────────────────────────────────────────────────────────

class LiveTrader:
    """
    Executes real GTC limit orders on the Polymarket CLOB.

    Drop-in replacement for PaperTrader — same public interface:
      .buy(), .sell(), .resolve_position(), .process_settlements(), .stats()
      .balance, .positions, .get_position()

    Settlement is handled by polling the CLOB for fill status rather than
    a simulated timeout. Balance is refreshed from the chain every 5 minutes
    and after each completed sell.
    """

    def __init__(
        self,
        private_key: str,
        funder: str,
        host: str = POLY_HOST,
    ):
        if not private_key or not funder:
            raise ValueError(
                "POLY_PRIVATE_KEY and POLY_FUNDER must both be set "
                "before live trading can be enabled."
            )

        ClobClient, _, _, _, _ = _clob_imports()

        self._client = ClobClient(
            host,
            key=private_key,
            chain_id=POLY_CHAIN_ID,
            signature_type=POLY_SIG_TYPE,
            funder=funder,
        )
        # Derive API credentials from the wallet (creates them on-chain if needed)
        self._client.set_api_creds(self._client.create_or_derive_api_creds())

        # Local state mirror — source of truth is the CLOB; this is our cache
        self.state = EngineState(balance=0.0)
        self._pending_orders: Dict[str, Tuple[Order, Position]] = {}  # order_id → (order, pos)
        self._last_balance_ts: float = 0.0
        self._connected: bool = True
        self._funder_prefix: str = funder[:10]

        logger.info(
            "LiveTrader initialised | funder=%s… | host=%s",
            self._funder_prefix, host
        )

    # ── Balance / position interface ──────────────────────────────────────────

    @property
    def balance(self) -> float:
        return self.state.balance

    @property
    def positions(self) -> List[Position]:
        return self.state.positions

    def get_position(self, market_id: str, side: OrderSide) -> Optional[Position]:
        for p in self.state.positions:
            if p.market_id == market_id and p.side == side:
                return p
        return None

    async def refresh_balance(self) -> float:
        """Fetch real USDC balance from the CLOB and update local state."""
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, self._client.get_balance)
            # py-clob-client returns {"balance": "12.345678"} or a plain float/str
            if isinstance(resp, dict):
                usdc = float(resp.get("balance", 0) or 0)
            else:
                usdc = float(resp) if resp else 0.0
            self.state.balance = usdc
            self._last_balance_ts = time.time()
            logger.info("CLOB balance refreshed: $%.4f USDC", usdc)
            return usdc
        except Exception as exc:
            logger.warning("Balance refresh failed: %s", exc)
            return self.state.balance

    # ── Order placement ────────────────────────────────────────────────────────

    async def buy(
        self,
        market: MarketContext,
        side: OrderSide,
        dollar_amount: float,
        max_price: float,
        stop_loss: Optional[float] = None,
    ) -> Optional[Order]:
        """
        Submit a real GTC limit BUY order to the Polymarket CLOB.

        dollar_amount : USDC to spend
        max_price     : limit price per share (0.01 – 0.99)
        Returns Order on submission, None on failure (no balance, no liquidity, CLOB error).
        """
        _, OrderArgs, OrderType, BUY, _ = _clob_imports()

        tok = market.yes_token_id if side == OrderSide.YES else market.no_token_id
        if not tok:
            logger.warning("No token_id for %s %s — cannot place live order",
                           market.condition_id, side.value)
            return None

        # Convert dollar amount → share count (Polymarket: 1 share = $1 at resolution)
        shares = round(dollar_amount / max(max_price, 0.001), 4)
        if shares < 1.0:
            logger.info(
                "Order too small: %.4f shares (min 1.0) for %s",
                shares, market.condition_id
            )
            return None

        try:
            loop = asyncio.get_event_loop()

            order_args = OrderArgs(
                token_id=tok,
                price=max_price,
                size=shares,
                side=BUY,
            )

            # Sign the order (EIP-712, CPU-bound → executor)
            signed = await loop.run_in_executor(
                None, lambda: self._client.create_order(order_args)
            )
            # Submit to CLOB
            resp = await loop.run_in_executor(
                None, lambda: self._client.post_order(signed, OrderType.GTC)
            )

            order_id = self._parse_order_id(resp) or str(uuid.uuid4())[:8]

            order = Order(
                market_id    = market.condition_id,
                side         = side,
                price        = max_price,
                shares       = shares,
                status       = OrderStatus.PENDING,
                order_id     = order_id,
            )
            self.state.orders.append(order)

            # Optimistically reserve balance; reconcile after fill
            self.state.balance = max(0.0, self.state.balance - dollar_amount)

            pos = Position(
                market_id  = market.condition_id,
                side       = side,
                shares     = shares,
                avg_price  = max_price,     # updated on confirmed fill
                cost_basis = dollar_amount,
                settled    = False,
            )
            self._pending_orders[order_id] = (order, pos)

            logger.info(
                "LIVE BUY submitted | %s %s | %.4f shares @ %.4f | order=%s",
                market.condition_id, side.value, shares, max_price, order_id
            )
            return order

        except Exception as exc:
            logger.error("CLOB BUY failed for %s: %s", market.condition_id, exc, exc_info=True)
            return None

    async def sell(
        self,
        market: MarketContext,
        position: Position,
        min_price: float,
    ) -> Optional[Trade]:
        """
        Close a position by submitting a real GTC limit SELL to the CLOB.
        Can only sell once the position is settled (confirmed by CLOB).
        """
        _, OrderArgs, OrderType, _, SELL = _clob_imports()

        if not position.settled:
            logger.warning("Cannot sell unsettled position for %s", position.market_id)
            return None

        tok = market.yes_token_id if position.side == OrderSide.YES else market.no_token_id
        if not tok:
            return None

        try:
            loop = asyncio.get_event_loop()

            order_args = OrderArgs(
                token_id=tok,
                price=min_price,
                size=position.shares,
                side=SELL,
            )

            signed = await loop.run_in_executor(
                None, lambda: self._client.create_order(order_args)
            )
            resp = await loop.run_in_executor(
                None, lambda: self._client.post_order(signed, OrderType.GTC)
            )

            order_id = self._parse_order_id(resp) or str(uuid.uuid4())[:8]

            # Record trade optimistically at limit price
            revenue  = position.shares * min_price
            fee      = _calc_fee(min_price, position.shares)
            net      = revenue - fee
            net_pnl  = net - position.cost_basis
            pnl_pct  = net_pnl / position.cost_basis if position.cost_basis else 0.0

            trade = Trade(
                market_id   = market.condition_id,
                question    = market.question,
                side        = position.side,
                entry_price = position.avg_price,
                exit_price  = min_price,
                shares      = position.shares,
                gross_pnl   = revenue - position.cost_basis,
                fee_cost    = fee,
                net_pnl     = net_pnl,
                pnl_pct     = pnl_pct,
                duration_s  = time.time() - position.opened_at,
            )
            self.state.trade_log.append(trade)
            self.state.total_pnl += net_pnl
            if net_pnl >= 0:
                self.state.win_count  += 1
            else:
                self.state.loss_count += 1

            self.state.positions.remove(position)
            self.state.balance += net  # optimistic; reconciled on next refresh

            logger.info(
                "LIVE SELL submitted | %s %s | %.4f shares @ %.4f | PnL $%.4f | order=%s",
                market.condition_id, position.side.value,
                position.shares, min_price, net_pnl, order_id
            )

            # Reconcile balance after 15s
            loop.call_later(15, lambda: asyncio.ensure_future(self.refresh_balance()))
            return trade

        except Exception as exc:
            logger.error("CLOB SELL failed for %s: %s", position.market_id, exc, exc_info=True)
            return None

    async def resolve_position(
        self,
        position: Position,
        won: bool,
        market_id: str,
        question: str,
    ) -> Trade:
        """Settle a position at market resolution (1.0 win / 0.0 loss)."""
        exit_price = 1.0 if won else 0.0
        revenue    = position.shares * exit_price
        fee        = _calc_fee(exit_price, position.shares) if won else 0.0
        net        = revenue - fee
        self.state.balance += net

        net_pnl  = net - position.cost_basis
        pnl_pct  = net_pnl / position.cost_basis if position.cost_basis else 0.0

        trade = Trade(
            market_id        = market_id,
            question         = question,
            side             = position.side,
            entry_price      = position.avg_price,
            exit_price       = exit_price,
            shares           = position.shares,
            gross_pnl        = revenue - position.cost_basis,
            fee_cost         = fee,
            net_pnl          = net_pnl,
            pnl_pct          = pnl_pct,
            duration_s       = time.time() - position.opened_at,
            resolved_outcome = "WIN" if won else "LOSS",
        )
        self.state.trade_log.append(trade)
        self.state.total_pnl += net_pnl
        if net_pnl >= 0:
            self.state.win_count  += 1
        else:
            self.state.loss_count += 1
        self.state.positions.remove(position)
        return trade

    # ── Settlement polling ─────────────────────────────────────────────────────

    async def process_settlements(self):
        """
        Poll the CLOB for fill status on all pending orders.
        Called every ~5s by TradingEngine._settlement_loop().

        Order lifecycle on Polymarket CLOB:
          LIVE → MATCHED → (on-chain) → CONFIRMED / FILLED
          UNMATCHED / CANCELLED / EXPIRED → refund reserved balance
        """
        if not self._pending_orders:
            # Refresh balance every 5 min even when idle
            if time.time() - self._last_balance_ts > 300:
                asyncio.ensure_future(self.refresh_balance())
            return

        loop = asyncio.get_event_loop()
        for order_id, (order, pos) in list(self._pending_orders.items()):
            try:
                resp = await loop.run_in_executor(
                    None, lambda oid=order_id: self._client.get_order(oid)
                )
                status = self._parse_status(resp)

                if status in ("MATCHED", "FILLED", "CONFIRMED"):
                    avg_price   = self._parse_avg_price(resp, order.price)
                    filled_size = self._parse_filled_size(resp, pos.shares)

                    order.status     = OrderStatus.CONFIRMED
                    order.settled_at = time.time()
                    order.filled_at  = avg_price
                    order.filled_size = filled_size

                    pos.settled    = True
                    pos.avg_price  = avg_price
                    pos.shares     = filled_size
                    pos.cost_basis = filled_size * avg_price

                    self.state.positions.append(pos)
                    del self._pending_orders[order_id]

                    logger.info(
                        "Order CONFIRMED | %s | %s %s %.4f @ %.4f",
                        order_id, pos.market_id, pos.side.value, filled_size, avg_price
                    )

                elif status in ("CANCELLED", "EXPIRED", "UNMATCHED"):
                    # Refund optimistically-deducted balance
                    self.state.balance += pos.cost_basis
                    order.status = OrderStatus.CANCELLED
                    del self._pending_orders[order_id]
                    logger.info(
                        "Order %s: %s | refunded $%.4f",
                        order_id, status, pos.cost_basis
                    )

            except Exception as exc:
                logger.debug("Settlement poll %s: %s", order_id[:12], exc)

    # ── CLOB response parsers ──────────────────────────────────────────────────

    @staticmethod
    def _parse_order_id(resp: Any) -> Optional[str]:
        """Extract order ID from py-clob-client response (dict or object)."""
        if isinstance(resp, dict):
            return (resp.get("orderID")
                    or resp.get("order_id")
                    or resp.get("id"))
        for attr in ("orderID", "order_id", "id"):
            if hasattr(resp, attr):
                return getattr(resp, attr)
        return None

    @staticmethod
    def _parse_status(resp: Any) -> str:
        """Extract order status string, uppercased."""
        if isinstance(resp, dict):
            raw = (resp.get("status")
                   or resp.get("order_status")
                   or "LIVE")
            return str(raw).upper()
        for attr in ("status", "order_status"):
            if hasattr(resp, attr):
                return str(getattr(resp, attr)).upper()
        return "LIVE"

    @staticmethod
    def _parse_avg_price(resp: Any, fallback: float) -> float:
        if isinstance(resp, dict):
            val = (resp.get("avg_price")
                   or resp.get("average_price")
                   or resp.get("price"))
            return float(val) if val else fallback
        for attr in ("avg_price", "average_price", "price"):
            if hasattr(resp, attr):
                return float(getattr(resp, attr))
        return fallback

    @staticmethod
    def _parse_filled_size(resp: Any, fallback: float) -> float:
        if isinstance(resp, dict):
            val = (resp.get("size_matched")
                   or resp.get("filled_size")
                   or resp.get("size_filled"))
            return float(val) if val else fallback
        for attr in ("size_matched", "filled_size", "size_filled"):
            if hasattr(resp, attr):
                return float(getattr(resp, attr))
        return fallback

    # ── Cancel helpers ─────────────────────────────────────────────────────────

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending GTC order. Returns True if the CLOB accepted."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self._client.cancel_order(order_id)
            )
            if order_id in self._pending_orders:
                _, pos = self._pending_orders.pop(order_id)
                self.state.balance += pos.cost_basis   # refund
            logger.info("Order %s cancelled", order_id)
            return True
        except Exception as exc:
            logger.warning("Cancel order %s failed: %s", order_id, exc)
            return False

    async def cancel_all_orders(self) -> int:
        """Cancel all pending orders. Returns count cancelled."""
        ids = list(self._pending_orders.keys())
        cancelled = 0
        for oid in ids:
            if await self.cancel_order(oid):
                cancelled += 1
        return cancelled

    # ── Connection health ──────────────────────────────────────────────────────

    async def health_check(self) -> Dict:
        """Test CLOB connectivity and return status dict."""
        try:
            loop = asyncio.get_event_loop()
            # get_balance is a lightweight ping-style call
            await loop.run_in_executor(None, self._client.get_balance)
            self._connected = True
        except Exception as exc:
            self._connected = False
            logger.warning("CLOB health check failed: %s", exc)

        return {
            "connected":          self._connected,
            "funder":             self._funder_prefix + "…",
            "host":               POLY_HOST,
            "balance_usd":        round(self.state.balance, 4),
            "pending_orders":     len(self._pending_orders),
            "last_balance_ts":    int(self._last_balance_ts),
        }

    # ── Stats (mirrors PaperTrader.stats) ─────────────────────────────────────

    def stats(self) -> Dict:
        return {
            "balance":          round(self.state.balance, 4),
            "total_pnl":        round(self.state.total_pnl, 4),
            "total_trades":     self.state.total_trades,
            "win_count":        self.state.win_count,
            "loss_count":       self.state.loss_count,
            "win_rate":         round(self.state.win_rate, 4),
            "open_positions":   len(self.state.positions),
            "pending_orders":   len(self._pending_orders),
            "portfolio_value":  round(self.state.portfolio_value, 4),
            "is_live":          True,
            "connected":        self._connected,
            "last_balance_ts":  int(self._last_balance_ts),
            "trade_log": [
                {
                    "market_id":  t.market_id,
                    "question":   t.question[:80],
                    "side":       t.side.value,
                    "entry":      round(t.entry_price, 4),
                    "exit":       round(t.exit_price, 4) if t.exit_price is not None else None,
                    "net_pnl":    round(t.net_pnl, 4),
                    "pnl_pct":    round(t.pnl_pct * 100, 2),
                    "signal":     t.signal_type,
                    "outcome":    t.resolved_outcome,
                    "ts":         int(t.timestamp),
                }
                for t in self.state.trade_log[-50:]
            ],
        }

    # ── Pending orders public accessor ─────────────────────────────────────────

    def get_pending_orders(self) -> List[Dict]:
        """Return list of pending (unconfirmed) CLOB orders."""
        result = []
        for order_id, (order, pos) in self._pending_orders.items():
            result.append({
                "order_id":   order_id,
                "market_id":  order.market_id,
                "side":       order.side.value,
                "price":      order.price,
                "shares":     order.shares,
                "cost_basis": round(pos.cost_basis, 4),
                "status":     order.status.value,
                "created_at": int(order.created_at),
                "age_s":      int(time.time() - order.created_at),
            })
        return result


# ── Factory ────────────────────────────────────────────────────────────────────

def create_live_trader(
    private_key: str,
    funder: str,
    host: str = POLY_HOST,
) -> Optional[LiveTrader]:
    """
    Build a LiveTrader if credentials are present and py-clob-client is installed.
    Returns None (and logs a clear warning) if either precondition is missing —
    the caller should fall back to PaperTrader.
    """
    if not private_key or not funder:
        logger.warning(
            "Live trading disabled: POLY_PRIVATE_KEY and/or POLY_FUNDER not set in .env"
        )
        return None
    try:
        _clob_imports()
    except ImportError as exc:
        logger.warning("Live trading disabled: %s", exc)
        return None
    try:
        return LiveTrader(private_key=private_key, funder=funder, host=host)
    except Exception as exc:
        logger.error("LiveTrader init failed: %s", exc, exc_info=True)
        return None
