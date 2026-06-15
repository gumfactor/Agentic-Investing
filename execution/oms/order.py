"""Order data model and state machine.

State transitions:
    STAGED → PENDING → SUBMITTED → FILLED
                               ↘ PARTIALLY_FILLED → FILLED
                               ↘ REJECTED              ↘ REJECTED
                               ↘ CANCELLED             ↘ CANCELLED

STAGED           : order created by portfolio construction; not yet compliance-checked
PENDING          : compliance checks passed; ready for broker submission
SUBMITTED        : sent to broker; awaiting fill confirmation
PARTIALLY_FILLED : broker confirmed a partial fill; remainder still working
FILLED           : broker confirmed full fill (filled_quantity == quantity)
REJECTED         : compliance rejected or broker refused
CANCELLED        : operator cancelled before full fill
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    STAGED = "STAGED"
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


# Valid state transitions
_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.STAGED: {OrderStatus.PENDING, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.PENDING: {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {
        OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED,   # subsequent partial fills
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.FILLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.CANCELLED: set(),
}


@dataclass
class Order:
    """A single order in the OMS.

    All monetary values are USD.  Shares are whole numbers for equities.
    """

    ticker: str
    side: OrderSide
    quantity: float           # shares to trade
    limit_price: float | None = None   # None = market order
    strategy_id: str = ""
    notes: str = ""

    # Set by OMS, not caller
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: OrderStatus = OrderStatus.STAGED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    broker_order_id: str | None = None
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    rejection_reason: str = ""

    def transition(self, new_status: OrderStatus, reason: str = "") -> None:
        """Advance order to new_status; raises ValueError on illegal transition."""
        allowed = _TRANSITIONS[self.status]
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition {self.status} → {new_status} for order {self.order_id}"
            )
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc)
        if new_status == OrderStatus.REJECTED:
            self.rejection_reason = reason

    @property
    def is_terminal(self) -> bool:
        return self.status in {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}

    @property
    def is_partial(self) -> bool:
        return self.status == OrderStatus.PARTIALLY_FILLED

    @property
    def fill_fraction(self) -> float:
        """Fraction of the order filled so far (0.0–1.0)."""
        if self.quantity <= 0:
            return 0.0
        return min(self.filled_quantity / self.quantity, 1.0)

    @property
    def notional(self) -> float | None:
        """Approximate notional; requires limit_price or avg_fill_price."""
        price = self.avg_fill_price or self.limit_price
        if price is None:
            return None
        return self.quantity * price

    def to_display_row(self) -> dict:
        """Return a dict suitable for tabular display to the operator (C1 confirmation)."""
        return {
            "order_id": self.order_id[:8],
            "ticker": self.ticker,
            "side": self.side.value,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "status": self.status.value,
            "strategy_id": self.strategy_id,
            "notes": self.notes,
        }
