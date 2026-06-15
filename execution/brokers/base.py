"""Abstract broker interface.

All broker implementations (IBKR paper, IBKR live, simulation) must satisfy
this contract so the OMS can swap brokers without code changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

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

    @abstractmethod
    def get_fill(self, broker_order_id: str) -> dict | None:
        """Poll for fill status.

        Returns a dict with keys:
          - filled_quantity: float
          - avg_price: float
          - status: str  (e.g. 'Filled', 'PartiallyFilled', 'Submitted')

        Returns None if the order is not yet filled or status unknown.
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
