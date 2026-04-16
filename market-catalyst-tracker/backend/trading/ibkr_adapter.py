"""
Interactive Brokers Client Portal Web API Adapter
===================================================
Connects to the local IBKR Client Portal Gateway (localhost:5000).

Setup:
  1. Download gateway: https://www.interactivebrokers.com/en/trading/ib-api.php
  2. Run: bin/run.sh root/conf.yaml  (Mac/Linux)  or  bin\\run.bat  (Windows)
  3. Authenticate at https://localhost:5000 in your browser
  4. Set IBKR_GATEWAY_URL=https://localhost:5000 in .env (or leave default)

All calls go through https://localhost:5000/v1/api/...
SSL verification is disabled (self-signed cert) — this is expected for local gateway.

Paper trading: log in with your paper account credentials at the gateway.
Live trading:  log in with your live account credentials.

IMPORTANT: Gateway session expires after ~24h. You must re-authenticate
in the browser. For unattended bots, IBKR requires the OAuth 2.0 flow
(contact IBKR for credentials) — this adapter covers the Gateway approach.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Gateway URL — override via IBKR_GATEWAY_URL env var
_GATEWAY = os.getenv("IBKR_GATEWAY_URL", "https://localhost:5000")
_BASE    = f"{_GATEWAY}/v1/api"

# Disable SSL verification warnings for self-signed cert
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _get(path: str, params: Dict | None = None, timeout: int = 10) -> Any:
    url = f"{_BASE}{path}"
    try:
        r = httpx.get(url, params=params or {}, verify=False, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        raise ConnectionError(
            "Cannot reach IBKR gateway at localhost:5000. "
            "Is the Client Portal Gateway running? (bin/run.sh root/conf.yaml)"
        )
    except httpx.HTTPStatusError as e:
        logger.warning("IBKR GET %s → HTTP %d: %s", path, e.response.status_code, e.response.text[:200])
        raise
    except Exception as e:
        logger.debug("IBKR GET %s → %s", path, e)
        raise


def _post(path: str, payload: Dict | None = None, timeout: int = 15) -> Any:
    url = f"{_BASE}{path}"
    try:
        r = httpx.post(url, json=payload or {}, verify=False, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        raise ConnectionError("Cannot reach IBKR gateway at localhost:5000.")
    except httpx.HTTPStatusError as e:
        logger.warning("IBKR POST %s → HTTP %d: %s", path, e.response.status_code, e.response.text[:200])
        raise
    except Exception as e:
        logger.debug("IBKR POST %s → %s", path, e)
        raise


def _delete(path: str, timeout: int = 10) -> Any:
    url = f"{_BASE}{path}"
    try:
        r = httpx.delete(url, verify=False, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("IBKR DELETE %s → %s", path, e)
        raise


# ── Session / Auth ─────────────────────────────────────────────────────────────

def get_auth_status() -> Dict:
    """
    Check if the gateway session is authenticated.
    Returns {"authenticated": bool, "competing": bool, "connected": bool}
    """
    try:
        data = _get("/iserver/auth/status")
        return {
            "authenticated": data.get("authenticated", False),
            "competing":     data.get("competing", False),
            "connected":     data.get("connected", False),
            "message":       data.get("message", ""),
        }
    except ConnectionError as e:
        return {"authenticated": False, "connected": False, "error": str(e)}
    except Exception as e:
        return {"authenticated": False, "connected": False, "error": str(e)}


def reauthenticate() -> bool:
    """Ping the gateway to keep the session alive (call every 30-60 minutes)."""
    try:
        _post("/iserver/reauthenticate")
        return True
    except Exception:
        return False


def tickle() -> bool:
    """Keep session alive (ping). Call every 60s in a background task."""
    try:
        _post("/tickle")
        return True
    except Exception:
        return False


# ── Accounts ───────────────────────────────────────────────────────────────────

def get_accounts() -> List[str]:
    """Return list of account IDs available in the session."""
    try:
        data = _get("/iserver/accounts")
        return data.get("accounts", [])
    except Exception as e:
        logger.warning("get_accounts: %s", e)
        return []


def get_account_summary(account_id: str) -> Dict:
    """
    Account balance, buying power, net liquidation value.
    Key fields: TotalCashValue, NetLiquidation, BuyingPower, GrossPositionValue.
    """
    try:
        data = _get(f"/portfolio/{account_id}/summary")
        # Flatten the nested {key: {amount, currency}} structure
        return {k: v.get("amount") for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        logger.warning("get_account_summary %s: %s", account_id, e)
        return {}


def get_positions(account_id: str) -> List[Dict]:
    """
    Open positions.
    Fields: conid, ticker, position, mktPrice, mktValue, avgCost, unrealizedPnl, realizedPnl.
    """
    try:
        return _get(f"/portfolio/{account_id}/positions/0") or []
    except Exception as e:
        logger.warning("get_positions %s: %s", account_id, e)
        return []


def get_pnl(account_id: str) -> Dict:
    """Daily and unrealized P&L for the account."""
    try:
        return _get(f"/iserver/account/pnl/partitioned") or {}
    except Exception as e:
        logger.warning("get_pnl: %s", e)
        return {}


# ── Market Data ────────────────────────────────────────────────────────────────

def search_contract(symbol: str, sec_type: str = "STK") -> Optional[Dict]:
    """
    Resolve a ticker symbol to an IBKR contract ID (conid).
    sec_type: STK (stock), OPT (option), FUT (future), CASH (forex), CRYPTO
    """
    try:
        results = _get("/iserver/secdef/search", {"symbol": symbol, "secType": sec_type})
        if results:
            r = results[0]
            return {
                "conid":       r.get("conid"),
                "symbol":      r.get("symbol"),
                "company":     r.get("companyName"),
                "exchange":    r.get("listingExchange"),
                "currency":    r.get("currency"),
                "sec_type":    r.get("secType"),
            }
    except Exception as e:
        logger.warning("search_contract %s: %s", symbol, e)
    return None


def get_market_snapshot(conids: List[int], fields: Optional[List[str]] = None) -> List[Dict]:
    """
    Real-time market data snapshot for one or more contracts.
    Default fields: last price, bid, ask, volume, change%, open, high, low, 52w-high/low.

    IBKR field codes:
      31=last, 84=bid, 86=ask, 87=ask_size, 88=bid_size,
      7295=52w_high, 7296=52w_low, 7741=has_options
      6509=market_cap, 7636=implied_vol
    """
    if fields is None:
        fields = ["31", "84", "86", "87", "88", "7295", "7296", "83", "85", "70", "71", "72", "73"]

    try:
        conid_str = ",".join(str(c) for c in conids)
        field_str = ",".join(fields)
        data = _get("/iserver/marketdata/snapshot", {"conids": conid_str, "fields": field_str})
        return data or []
    except Exception as e:
        logger.warning("get_market_snapshot: %s", e)
        return []


def get_stock_quote(symbol: str) -> Optional[Dict]:
    """Convenience: search for symbol then get a live snapshot."""
    contract = search_contract(symbol)
    if not contract or not contract.get("conid"):
        return None
    conid = contract["conid"]
    snaps = get_market_snapshot([conid])
    if not snaps:
        return None
    s = snaps[0]
    return {
        "symbol":   symbol,
        "conid":    conid,
        "last":     s.get("31"),
        "bid":      s.get("84"),
        "ask":      s.get("86"),
        "open":     s.get("7295"),   # approximation
        "high_52w": s.get("7295"),
        "low_52w":  s.get("7296"),
        "change_pct": s.get("83"),
    }


# ── Orders ─────────────────────────────────────────────────────────────────────

def place_order(
    account_id:  str,
    conid:       int,
    action:      str,           # "BUY" or "SELL"
    quantity:    float,
    order_type:  str = "MKT",   # "MKT", "LMT", "STP", "STP LMT"
    limit_price: Optional[float] = None,
    stop_price:  Optional[float] = None,
    tif:         str = "DAY",   # "DAY", "GTC", "IOC"
    outside_rth: bool = False,  # Allow pre/after-hours execution
) -> Dict:
    """
    Place a stock order via the Client Portal API.

    Returns order confirmation dict with orderId.
    May require a confirmation reply if IBKR shows a warning message.

    Example:
        place_order(acct, 265598, "BUY", 10, "LMT", limit_price=150.00)
    """
    order = {
        "conid":       conid,
        "secType":     f"{conid}:STK",
        "orderType":   order_type,
        "side":        action.upper(),
        "quantity":    quantity,
        "tif":         tif,
        "outsideRTH":  outside_rth,
    }
    if limit_price is not None:
        order["price"] = limit_price
    if stop_price is not None:
        order["auxPrice"] = stop_price

    try:
        result = _post(f"/iserver/account/{account_id}/orders", {"orders": [order]})
        # Gateway may return a list of replies requiring confirmation
        if isinstance(result, list):
            # Check for "question" requiring confirmation
            if result and result[0].get("id"):
                confirmed = confirm_order(result[0]["id"])
                return confirmed[0] if confirmed else result[0]
            return result[0] if result else {}
        return result
    except Exception as e:
        logger.error("place_order %s %s %s: %s", action, quantity, conid, e)
        raise


def confirm_order(reply_id: str) -> List[Dict]:
    """
    Confirm a pending order that requires acknowledgement (e.g. outside RTH warning).
    Returns the final order confirmation.
    """
    try:
        return _post(f"/iserver/reply/{reply_id}", {"confirmed": True}) or []
    except Exception as e:
        logger.warning("confirm_order %s: %s", reply_id, e)
        return []


def get_open_orders() -> List[Dict]:
    """Return all open/pending orders across all accounts."""
    try:
        data = _get("/iserver/account/orders")
        return data.get("orders", []) if isinstance(data, dict) else (data or [])
    except Exception as e:
        logger.warning("get_open_orders: %s", e)
        return []


def cancel_order(account_id: str, order_id: str) -> Dict:
    """Cancel an open order by order ID."""
    try:
        return _delete(f"/iserver/account/{account_id}/order/{order_id}")
    except Exception as e:
        logger.warning("cancel_order %s: %s", order_id, e)
        return {}


# ── Portfolio helpers ──────────────────────────────────────────────────────────

def get_portfolio_snapshot(account_id: str) -> Dict:
    """
    Full portfolio snapshot: summary + positions combined.
    Returns dict ready for the frontend portfolio view.
    """
    summary   = get_account_summary(account_id)
    positions = get_positions(account_id)

    return {
        "account_id":       account_id,
        "net_liquidation":  summary.get("NetLiquidation"),
        "cash":             summary.get("TotalCashValue"),
        "buying_power":     summary.get("BuyingPower"),
        "gross_position":   summary.get("GrossPositionValue"),
        "unrealized_pnl":   sum(p.get("unrealizedPnl", 0) or 0 for p in positions),
        "realized_pnl":     sum(p.get("realizedPnl", 0) or 0 for p in positions),
        "positions":        positions,
        "position_count":   len(positions),
    }
