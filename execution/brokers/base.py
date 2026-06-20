"""Abstract broker interface.

All broker implementations (IBKR paper, IBKR live, simulation) must satisfy
this contract so the OMS can swap brokers without code changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from execution.oms.order import Order


class BaseBroker(ABC):
    """Minimal broker contract used by OrderManager."""

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the broker gateway."""

    @abstractmethod
    def disconnect(self) -> None:
        """Cleanly close the broker connection."""

    @abstractmethod
    def submit_order(self, order: Order) -> str:
        """Submit an order to the broker.

        Returns a broker-assigned order ID string.

        Raises
        ------
        RuntimeError if the broker rejects the order at the API level.
        """

    def what_if_order(self, order: Order) -> dict:
        """Validate an order with the broker without transmitting it.

        Broker implementations that support a paper what-if path should
        override this. The default fails closed so callers cannot accidentally
        treat missing broker support as approval.
        """
        raise NotImplementedError("Broker does not support what-if order validation")

    @abstractmethod
    def get_fill(self, broker_order_id: str) -> dict | None:
        """Poll for fill status.

        Returns a dict with keys:
          - filled_quantity: float
          - avg_price: float
          - status: str  (e.g. 'Filled', 'PartiallyFilled', 'Submitted')

        Returns None if the order is not yet filled or status unknown.
        """

    def get_order_status(self, broker_order_id: str) -> dict | None:
        """Fetch durable broker order/fill status without mutating the order.

        Implementations should query broker state by broker order id and must
        not submit, cancel, or otherwise mutate orders. The default fails
        closed so durable reconciliation callers only use brokers that
        explicitly support this read-only lookup.
        """
        raise NotImplementedError("Broker does not support durable order status lookup")

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a live order at the broker.

        Returns True if the cancellation request was accepted.
        The order may not be immediately cancelled — check get_fill() for final status.
        """

    @abstractmethod
    def get_positions(self) -> dict[str, float]:
        """Return current broker positions as {ticker: shares}."""

    @abstractmethod
    def get_account_value(self) -> float:
        """Return total account value in USD."""

    @property
    @abstractmethod
    def is_paper(self) -> bool:
        """True when connected to the paper trading environment."""
