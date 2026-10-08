"""
Paper broker — the only execution venue in this system.

* Idempotent: client_order_id is derived from the proposal id, so re-submitting
  the same approval returns the existing order instead of creating a new one.
* Duplicate detection: a second working order or open position on the same
  legs is rejected.
* Partial fills are tracked (filled_contracts < contracts → partially_filled)
  and block new entries until resolved.
* Conservative fill model: an open fills only when the NATURAL credit
  (short bid − long ask) reaches the limit; it fills at the limit, never better.
* Ledger: cash, collateral (width × 100 per open contract), realized P&L.
  `reconcile()` rebuilds the ledger from the fill journal and compares.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from qqq.models import (CreditSpread, ExitPlan, OptionQuote, PaperOrder, PaperPosition,
                        TradeProposal, utcnow)
from qqq.rules import CONTRACT_MULTIPLIER, STARTING_BALANCE_USD, FeeModel
from qqq.store import Store

ET = ZoneInfo("America/New_York")
ACTIVE = ("working", "partially_filled")


class OrderRejected(RuntimeError):
    pass


class PaperBroker:
    actor = "paper_broker"

    def __init__(self, store: Store, fees: FeeModel, starting_balance: float = STARTING_BALANCE_USD):
        self.store = store
        self.fees = fees
        if self.store.kv_get("paper_starting_balance") is None:
            self.store.kv_set("paper_starting_balance", starting_balance)

    # ── queries ───────────────────────────────────────────────────────────────

    def orders(self, limit: int = 200) -> List[PaperOrder]:
        return [PaperOrder(**d) for d in self.store.list("orders", limit)]

    def active_orders(self) -> List[PaperOrder]:
        return [o for o in self.orders(1000) if o.status in ACTIVE]

    def positions(self, status: Optional[str] = None) -> List[PaperPosition]:
        out = [PaperPosition(**d) for d in self.store.list("positions", 1000)]
        return [p for p in out if status is None or p.status == status]

    def position(self, pid: str) -> Optional[PaperPosition]:
        d = self.store.get("positions", pid)
        return PaperPosition(**d) if d else None

    def fills(self) -> List[Dict]:
        return self.store.list("fills", 10000, newest_first=False)

    # ── ledger ────────────────────────────────────────────────────────────────

    def _ledger_from_fills(self) -> Tuple[float, Dict[str, int]]:
        cash = float(self.store.kv_get("paper_starting_balance", STARTING_BALANCE_USD))
        open_qty: Dict[str, int] = {}
        for f in self.fills():
            gross = f["price"] * CONTRACT_MULTIPLIER * f["qty"]
            if f["intent"] == "open":
                cash += gross - f["fees"]
                open_qty[f["position_id"]] = open_qty.get(f["position_id"], 0) + f["qty"]
            else:
                cash -= gross + f["fees"]
                open_qty[f["position_id"]] = open_qty.get(f["position_id"], 0) - f["qty"]
        return round(cash, 2), {k: v for k, v in open_qty.items() if v != 0}

    def cash(self) -> float:
        return float(self.store.kv_get("paper_cash", self.store.kv_get("paper_starting_balance",
                                                                        STARTING_BALANCE_USD)))

    def collateral(self) -> float:
        return round(sum(p.width * CONTRACT_MULTIPLIER * p.contracts for p in self.positions("open")), 2)

    def available_cash(self) -> float:
        return round(self.cash() - self.collateral(), 2)

    def realized_pnl_on(self, day: date) -> float:
        return round(sum(p.realized_pnl or 0 for p in self.positions()
                         if p.closed_at and p.closed_at.astimezone(ET).date() == day), 2)

    def reconcile(self) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        cash, open_qty = self._ledger_from_fills()
        if abs(cash - self.cash()) > 0.005:
            issues.append(f"cash mismatch: ledger {self.cash():.2f} vs fills {cash:.2f}")
        book = {p.id: p.contracts for p in self.positions("open")}
        if book != open_qty:
            issues.append(f"position mismatch: book {book} vs fills {open_qty}")
        for o in self.active_orders():
            if o.status == "partially_filled":
                issues.append(f"order {o.client_order_id} partially filled "
                              f"({o.filled_contracts}/{o.contracts}) — needs manual resolution")
        return (not issues), issues

    # ── order entry ───────────────────────────────────────────────────────────

    def submit_open(self, proposal: TradeProposal, spread: CreditSpread,
                    now: Optional[datetime] = None) -> PaperOrder:
        coid = f"open-{proposal.id}"
        existing = self.store.get("orders", coid)
        if existing:
            return PaperOrder(**existing)          # idempotent replay
        for o in self.active_orders():
            if {o.short_symbol, o.long_symbol} == {spread.short.osi(), spread.long.osi()}:
                raise OrderRejected(f"duplicate: working order {o.client_order_id} on the same legs")
        for p in self.positions("open"):
            if {p.short_symbol, p.long_symbol} == {spread.short.osi(), spread.long.osi()}:
                raise OrderRejected(f"duplicate: position {p.id} already open on the same legs")
        order = PaperOrder(
            client_order_id=coid, proposal_id=proposal.id, intent="open",
            position_id=f"pos-{proposal.id}", strategy=spread.strategy,
            short_symbol=spread.short.osi(), long_symbol=spread.long.osi(), expiry=spread.short.expiry,
            short_strike=spread.short.strike, long_strike=spread.long.strike, right=spread.right,
            contracts=spread.contracts, limit_price=spread.limit_credit,
            created_at=now or utcnow(), updated_at=now or utcnow(),
        )
        if not self.store.insert("orders", coid, order.model_dump(mode="json")):
            return PaperOrder(**self.store.get("orders", coid))
        self.store.kv_set(f"exit_plan:{order.position_id}",
                          proposal.exit_plan.model_dump(mode="json") if proposal.exit_plan else None)
        self.store.audit(self.actor, "order.submit", order.model_dump(mode="json"))
        return order

    def submit_close(self, proposal: TradeProposal, position: PaperPosition, limit_debit: float,
                     now: Optional[datetime] = None) -> PaperOrder:
        coid = f"close-{proposal.id}"
        existing = self.store.get("orders", coid)
        if existing:
            return PaperOrder(**existing)
        if position.status != "open":
            raise OrderRejected(f"position {position.id} is not open")
        for o in self.active_orders():
            if o.position_id == position.id and o.intent == "close":
                raise OrderRejected(f"duplicate: close order {o.client_order_id} already working")
        order = PaperOrder(
            client_order_id=coid, proposal_id=proposal.id, intent="close", position_id=position.id,
            strategy=position.strategy, short_symbol=position.short_symbol, long_symbol=position.long_symbol,
            expiry=position.expiry, short_strike=position.short_strike, long_strike=position.long_strike,
            right=position.right, contracts=position.contracts, limit_price=round(limit_debit, 2),
            created_at=now or utcnow(), updated_at=now or utcnow(),
        )
        if not self.store.insert("orders", coid, order.model_dump(mode="json")):
            return PaperOrder(**self.store.get("orders", coid))
        self.store.audit(self.actor, "order.submit", order.model_dump(mode="json"))
        return order

    def cancel(self, coid: str, by: str) -> PaperOrder:
        o = PaperOrder(**self.store.get("orders", coid))
        if o.status not in ACTIVE:
            return o
        o = o.model_copy(update={"status": "cancelled", "updated_at": utcnow(),
                                 "note": f"cancelled by {by}; {o.filled_contracts} filled"})
        self.store.upsert("orders", coid, o.model_dump(mode="json"))
        self.store.audit(by, "order.cancel", {"client_order_id": coid})
        return o

    # ── fills ─────────────────────────────────────────────────────────────────

    def try_fill(self, coid: str, short_q: OptionQuote, long_q: OptionQuote,
                 now: Optional[datetime] = None) -> PaperOrder:
        o = PaperOrder(**self.store.get("orders", coid))
        if o.status not in ACTIVE:
            return o
        if o.intent == "open":
            natural = short_q.bid - long_q.ask
            marketable = natural >= o.limit_price - 1e-9
        else:
            natural = short_q.ask - long_q.bid
            marketable = natural <= o.limit_price + 1e-9
        if not marketable:
            return o
        return self.apply_fill(coid, o.contracts - o.filled_contracts, o.limit_price, now)

    def apply_fill(self, coid: str, qty: int, price: float, now: Optional[datetime] = None,
                   fees_override: Optional[float] = None) -> PaperOrder:
        now = now or utcnow()
        o = PaperOrder(**self.store.get("orders", coid))
        if o.status not in ACTIVE:
            raise OrderRejected(f"order {coid} is {o.status}")
        remaining = o.contracts - o.filled_contracts
        if qty <= 0 or qty > remaining:
            raise OrderRejected(f"fill qty {qty} invalid; {remaining} remaining (over-fill blocked)")
        seq = self.store.count("fills") + 1
        fill_id = f"{coid}:{seq}:{uuid.uuid4().hex[:6]}"
        fees = round(2 * qty * self.fees.per_contract(), 2) if fees_override is None else fees_override
        fill = {"id": fill_id, "client_order_id": coid, "intent": o.intent, "position_id": o.position_id,
                "qty": qty, "price": price, "fees": fees, "at": now.isoformat()}
        self.store.insert("fills", fill_id, fill)

        filled = o.filled_contracts + qty
        avg = price if o.avg_fill_price is None else (
            (o.avg_fill_price * o.filled_contracts + price * qty) / filled)
        o = o.model_copy(update={"filled_contracts": filled, "avg_fill_price": round(avg, 4),
                                 "status": "filled" if filled == o.contracts else "partially_filled",
                                 "updated_at": now})
        self.store.upsert("orders", coid, o.model_dump(mode="json"))

        cash = self.cash()
        if o.intent == "open":
            cash += price * CONTRACT_MULTIPLIER * qty - fees
            self._book_open(o, qty, price, fees, now)
        else:
            cash -= price * CONTRACT_MULTIPLIER * qty + fees
            self._book_close(o, qty, price, fees, now)
        self.store.kv_set("paper_cash", round(cash, 2))
        self.store.audit(self.actor, "order.fill", fill)
        return o

    def _book_open(self, o: PaperOrder, qty: int, price: float, fees: float, now: datetime) -> None:
        existing = self.position(o.position_id)
        if existing:
            n = existing.contracts + qty
            pos = existing.model_copy(update={
                "contracts": n, "entry_fees": round(existing.entry_fees + fees, 2),
                "entry_credit": round((existing.entry_credit * existing.contracts + price * qty) / n, 4)})
        else:
            plan = self.store.kv_get(f"exit_plan:{o.position_id}")
            pos = PaperPosition(
                id=o.position_id, strategy=o.strategy, short_symbol=o.short_symbol,
                long_symbol=o.long_symbol, expiry=o.expiry, short_strike=o.short_strike,
                long_strike=o.long_strike, right=o.right, contracts=qty, entry_credit=price,
                entry_fees=fees, opened_at=now, proposal_id=o.proposal_id,
                exit_plan=ExitPlan(**plan) if plan else None)
        self.store.upsert("positions", pos.id, pos.model_dump(mode="json"))

    def _book_close(self, o: PaperOrder, qty: int, price: float, fees: float, now: datetime) -> None:
        pos = self.position(o.position_id)
        if pos is None:
            raise OrderRejected(f"no position {o.position_id} to close")
        realized_part = (pos.entry_credit - price) * CONTRACT_MULTIPLIER * qty - fees \
            - pos.entry_fees * qty / max(pos.contracts, 1)
        remaining = pos.contracts - qty
        upd = {"contracts": remaining, "exit_fees": round(pos.exit_fees + fees, 2),
               "realized_pnl": round((pos.realized_pnl or 0) + realized_part, 2),
               "exit_debit": price,
               "entry_fees": round(pos.entry_fees * remaining / max(pos.contracts, 1), 2)}
        if remaining == 0:
            upd.update({"status": "closed", "closed_at": now})
        self.store.upsert("positions", pos.id, pos.model_copy(update=upd).model_dump(mode="json"))

    # ── expiration ────────────────────────────────────────────────────────────

    @staticmethod
    def intrinsic_debit(pos: PaperPosition, underlying_close: float) -> float:
        if pos.right == "P":
            v = max(0.0, pos.short_strike - underlying_close) - max(0.0, pos.long_strike - underlying_close)
        else:
            v = max(0.0, underlying_close - pos.short_strike) - max(0.0, underlying_close - pos.long_strike)
        return round(min(max(v, 0.0), pos.width), 4)

    def settle_expiration(self, pos: PaperPosition, underlying_close: float, now: Optional[datetime] = None) -> PaperPosition:
        """Cash-equivalent settlement at intrinsic. Real QQQ options are physically settled —
        an ITM short leg would be ASSIGNED as 100 shares per contract (~$75k notional),
        far beyond a $250 account. This is flagged as an exit reason well before expiry."""
        now = now or utcnow()
        debit = self.intrinsic_debit(pos, underlying_close)
        coid = f"expire-{pos.id}"
        if not self.store.get("orders", coid):
            order = PaperOrder(client_order_id=coid, proposal_id=pos.proposal_id, intent="close",
                               position_id=pos.id, strategy=pos.strategy, short_symbol=pos.short_symbol,
                               long_symbol=pos.long_symbol, expiry=pos.expiry, short_strike=pos.short_strike,
                               long_strike=pos.long_strike, right=pos.right, contracts=pos.contracts,
                               limit_price=debit, note=f"expiration settlement @ {underlying_close}")
            self.store.insert("orders", coid, order.model_dump(mode="json"))
        # Worthless expiry: no closing commissions. ITM: assignment/exercise fees per contract.
        fees = 0.0 if debit == 0 else round(2 * pos.contracts * self.fees.assignment_fee, 2)
        self.apply_fill(coid, pos.contracts, debit, now, fees_override=fees)
        return self.position(pos.id)
