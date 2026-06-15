"""Interactive Brokers broker implementation using ib_insync.

Paper trading:  IBKR_PORT=7497  (TWS Paper or IB Gateway Paper)
Live trading:   IBKR_PORT=7496  (TWS Live or IB Gateway Live)

Safety rules enforced here:
- C1: order submission blocked unless caller has already obtained "YES"
      (enforced by OrderManager, not here — this class blindly submits)
- C8: raises EnvironmentError if PAPER_TRADING=false and 4-week paper-run
      gate has not been cleared (checked via env var PAPER_RUN_CLEARED)
- C9: the live vs. paper switch is governed entirely by IBKR_PORT +
      PAPER_TRADING env vars; never hardcoded.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import structlog

from execution.brokers.base import BaseBroker
from execution.oms.order import Order, OrderSide

logger = structlog.get_logger(__name__)

try:
    from ib_insync import IB, LimitOrder, MarketOrder, Stock
    _IB_AVAILABLE = True
except ImportError:
    _IB_AVAILABLE = False
    logger.warning("ib_insync_not_installed", advice="pip install ib-insync")


class IBKRBroker(BaseBroker):
    """IBKR TWS/Gateway broker via ib_insync.

    Parameters
    ----------
    host:
        IB Gateway / TWS host.  Defaults to IBKR_HOST env var or 127.0.0.1.
    port:
        7497 = paper, 7496 = live.  Defaults to IBKR_PORT env var.
    client_id:
        Unique client ID for this connection (default 1).
    timeout:
        Connection timeout in seconds.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        client_id: int = 1,
        timeout: int = 10,
    ) -> None:
        if not _IB_AVAILABLE:
            raise ImportError("ib_insync is required. `pip install ib-insync`")

        self._host = host or os.environ.get("IBKR_HOST", "127.0.0.1")
        raw_port = port or int(os.environ.get("IBKR_PORT", "7497"))
        self._port = raw_port
        self._client_id = client_id
        self._timeout = timeout
        self._ib: Optional["IB"] = None
        self._submitted: dict[str, object] = {}  # broker_order_id → ib Trade

        self._validate_paper_trading_flag()

    def _validate_paper_trading_flag(self) -> None:
        """Enforce C8: live trading gate requires 4 weeks of clean paper run."""
        paper_env = os.environ.get("PAPER_TRADING", "true").lower()
        if paper_env == "false" and self._port != 7496:
            raise EnvironmentError(
                "PAPER_TRADING=false but IBKR_PORT is not 7496.  "
                "Set IBKR_PORT=7496 for live trading (C9)."
            )
        if paper_env == "false":
            cleared = os.environ.get("PAPER_RUN_CLEARED", "false").lower()
            if cleared != "true":
                raise EnvironmentError(
                    "Live trading requires PAPER_RUN_CLEARED=true.  "
                    "Confirm 4 consecutive weeks of clean paper trading before switching (C8)."
                )

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        self._ib = IB()
        self._ib.connect(self._host, self._port, clientId=self._client_id, timeout=self._timeout)
        logger.info(
            "ibkr_connected",
            host=self._host,
            port=self._port,
            paper=self.is_paper,
        )

    def disconnect(self) -> None:
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
            logger.info("ibkr_disconnected")
        self._ib = None

    # ── Order submission ──────────────────────────────────────────────────────

    def submit_order(self, order: Order) -> str:
        self._require_connection()
        contract = Stock(order.ticker, "SMART", "USD")
        action = "BUY" if order.side == OrderSide.BUY else "SELL"

        if order.limit_price is not None:
            ib_order = LimitOrder(action, order.quantity, order.limit_price)
        else:
            ib_order = MarketOrder(action, order.quantity)

        trade = self._ib.placeOrder(contract, ib_order)
        self._ib.sleep(0.1)  # allow TWS to assign an orderId

        broker_id = str(trade.order.orderId)
        self._submitted[broker_id] = trade
        logger.info(
            "ibkr_order_placed",
            broker_id=broker_id,
            ticker=order.ticker,
            side=action,
            quantity=order.quantity,
            limit=order.limit_price,
        )
        return broker_id

    # ── Fill polling ──────────────────────────────────────────────────────────

    def get_fill(self, broker_order_id: str) -> dict | None:
        self._require_connection()
        trade = self._submitted.get(broker_order_id)
        if trade is None:
            return None

        self._ib.sleep(0)  # pump event loop
        status = trade.orderStatus.status
        filled = trade.orderStatus.filled
        avg_price = trade.orderStatus.avgFillPrice

        if status in ("Filled",) and filled > 0:
            return {
                "filled_quantity": float(filled),
                "avg_price": float(avg_price),
                "status": status,
            }
        return None

    # ── Account state ─────────────────────────────────────────────────────────

    def get_positions(self) -> dict[str, float]:
        self._require_connection()
        self._ib.sleep(0)
        positions: dict[str, float] = {}
        for pos in self._ib.positions():
            if hasattr(pos.contract, "symbol"):
                positions[pos.contract.symbol] = float(pos.position)
        return positions

    def get_account_value(self) -> float:
        self._require_connection()
        for av in self._ib.accountValues():
            if av.tag == "NetLiquidation" and av.currency == "USD":
                return float(av.value)
        return 0.0

    @property
    def is_paper(self) -> bool:
        return self._port == 7497

    def _require_connection(self) -> None:
        if self._ib is None or not self._ib.isConnected():
            raise RuntimeError(
                "Not connected to IBKR. Call connect() first."
            )
