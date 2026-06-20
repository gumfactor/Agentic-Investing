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

from datetime import date
import os
import time
from typing import Optional

_ORDER_ID_TIMEOUT_SECONDS = 5.0
_ORDER_ID_POLL_INTERVAL = 0.05
_FX_RATE_TIMEOUT_SECONDS = 5.0
_FX_RATE_POLL_INTERVAL = 0.1
_CONFIGURED_FX_RATE_MAX_AGE_DAYS = 1

import structlog

from execution.brokers.base import BaseBroker
from execution.oms.order import Order, OrderSide

logger = structlog.get_logger(__name__)

try:
    from ib_insync import Forex, IB, LimitOrder, MarketOrder, Stock
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
        """Enforce C8/C9: live trading gate requires explicit opt-in + 4-week paper run.

        Rules:
        - Port 7496 (live) requires PAPER_TRADING=false explicitly set.
          An unset PAPER_TRADING env var defaults to paper mode even if port=7496
          was passed — prevents accidental live connection (C9).
        - PAPER_TRADING=false requires PAPER_RUN_CLEARED=true (C8).
        - PAPER_TRADING=false with port != 7496 is a configuration error (C9).
        """
        paper_env = os.environ.get("PAPER_TRADING", "true").lower()

        # Block live port unless PAPER_TRADING is explicitly disabled
        if self._port == 7496 and paper_env != "false":
            raise EnvironmentError(
                f"IBKR_PORT={self._port} (live) requires PAPER_TRADING=false to be explicitly set. "
                "Default/unset PAPER_TRADING is treated as paper mode (C9)."
            )
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
        # Re-validate env vars at connection time; they may have changed since __init__
        self._validate_paper_trading_flag()
        if self._ib is not None and self._ib.isConnected():
            logger.warning("ibkr_already_connected", host=self._host, port=self._port)
            return
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

        # Poll the event loop until TWS assigns a real orderId (0 = not yet assigned).
        # Using ib.sleep() pumps the asyncio event loop so the callback can fire.
        deadline = time.time() + _ORDER_ID_TIMEOUT_SECONDS
        while trade.order.orderId == 0:
            if time.time() > deadline:
                raise RuntimeError(
                    f"IBKR did not assign an orderId within {_ORDER_ID_TIMEOUT_SECONDS}s "
                    f"for {order.ticker} {action} {order.quantity}. "
                    "Order may still be live at broker — check TWS before retrying."
                )
            self._ib.sleep(_ORDER_ID_POLL_INTERVAL)

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
        filled_qty = trade.orderStatus.filled
        avg_price = trade.orderStatus.avgFillPrice

        if status == "Filled":
            result = {"filled_quantity": filled_qty, "avg_price": avg_price, "status": "Filled"}
            # Evict completed order from tracking dict
            del self._submitted[broker_order_id]
            return result
        elif status in {"Cancelled", "Inactive"}:
            del self._submitted[broker_order_id]
            return None
        elif status == "PartiallyFilled" and filled_qty > 0:
            return {"filled_quantity": filled_qty, "avg_price": avg_price, "status": "PartiallyFilled"}
        return None

    # ── Order cancellation ───────────────────────────────────────────────────

    def cancel_order(self, broker_order_id: str) -> bool:
        """Request cancellation of a live order."""
        self._require_connection()
        trade = self._submitted.get(broker_order_id)
        if trade is None:
            logger.warning("ibkr_cancel_unknown_order", broker_order_id=broker_order_id)
            return False
        self._ib.cancelOrder(trade.order)
        logger.info("ibkr_cancel_requested", broker_order_id=broker_order_id, ticker=trade.contract.symbol)
        return True

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
        return self.get_account_value_in_currency("USD")

    def get_account_value_in_currency(self, currency: str) -> float:
        """Return total NetLiquidation converted into ``currency``."""
        self._require_connection()
        target_currency = self._validate_currency_code(currency, "reporting currency")
        values = self.get_account_values_by_currency()

        total = 0.0
        for source_currency, value in values.items():
            if source_currency == target_currency:
                total += value
            else:
                total += value * self._get_fx_rate(source_currency, target_currency)
        return total

    def get_account_values_by_currency(self) -> dict[str, float]:
        """Return account NetLiquidation components keyed by currency."""
        self._require_connection()
        self._ib.sleep(0)
        net_liquidation: dict[str, float] = {}
        ledger_values: dict[str, float] = {}
        for av in self._ib.accountValues():
            currency = self._normalize_currency(av.currency)
            if not currency or currency == "BASE":
                continue
            if av.tag == "NetLiquidation":
                net_liquidation[currency] = float(av.value)
            elif av.tag == "$LEDGER-NetLiquidationByCurrency":
                ledger_values[currency] = float(av.value)

        if len(ledger_values) > 1:
            return ledger_values
        if net_liquidation:
            return net_liquidation
        if ledger_values:
            return ledger_values

        raise RuntimeError(
            "NetLiquidation not found in IBKR account values for any currency. "
            "Position data may not have arrived yet; retry after a short delay."
        )

    @property
    def is_paper(self) -> bool:
        return self._port == 7497

    def _get_fx_rate(self, source_currency: str, target_currency: str) -> float:
        source = self._validate_currency_code(source_currency, "source currency")
        target = self._validate_currency_code(target_currency, "target currency")
        if source == target:
            return 1.0

        configured_rate = self._configured_fx_rate(source, target)
        if configured_rate is not None:
            return configured_rate

        direct_rate = self._request_fx_rate(f"{source}{target}")
        if direct_rate is not None:
            return direct_rate

        inverse_rate = self._request_fx_rate(f"{target}{source}")
        if inverse_rate is not None:
            return 1.0 / inverse_rate

        raise RuntimeError(
            f"Could not fetch FX rate {source}/{target} from IBKR. "
            "Cannot convert account NetLiquidation safely."
        )

    @classmethod
    def _configured_fx_rate(cls, source_currency: str, target_currency: str) -> float | None:
        source = cls._validate_currency_code(source_currency, "source currency")
        target = cls._validate_currency_code(target_currency, "target currency")

        direct = os.environ.get(f"IBKR_FX_RATE_{source}_{target}")
        if direct:
            cls._validate_configured_fx_rate_as_of(source, target)
            return cls._parse_positive_fx_rate(direct, f"{source}/{target}")

        inverse = os.environ.get(f"IBKR_FX_RATE_{target}_{source}")
        if inverse:
            cls._validate_configured_fx_rate_as_of(target, source)
            return 1.0 / cls._parse_positive_fx_rate(inverse, f"{target}/{source}")

        return None

    @classmethod
    def _validate_configured_fx_rate_as_of(cls, source_currency: str, target_currency: str) -> None:
        env_name = f"IBKR_FX_RATE_{source_currency}_{target_currency}_AS_OF"
        raw_as_of = os.environ.get(env_name)
        if not raw_as_of:
            raise RuntimeError(
                f"{env_name} is required when IBKR_FX_RATE_{source_currency}_{target_currency} is set. "
                "Use YYYY-MM-DD and refresh the manual FX rate before paper trading."
            )

        try:
            as_of = date.fromisoformat(raw_as_of)
        except ValueError as exc:
            raise RuntimeError(f"{env_name} must be a YYYY-MM-DD date; got {raw_as_of!r}") from exc

        today = cls._today()
        age_days = (today - as_of).days
        if age_days < 0:
            raise RuntimeError(f"{env_name} cannot be in the future; got {raw_as_of}")
        if age_days > _CONFIGURED_FX_RATE_MAX_AGE_DAYS:
            raise RuntimeError(
                f"{env_name} is stale by {age_days} days; manual FX rates must be refreshed "
                f"within {_CONFIGURED_FX_RATE_MAX_AGE_DAYS} day before paper trading."
            )

    @staticmethod
    def _parse_positive_fx_rate(raw: str, pair: str) -> float:
        try:
            rate = float(raw)
        except ValueError as exc:
            raise RuntimeError(f"Configured FX rate {pair} is not numeric: {raw!r}") from exc
        if rate <= 0:
            raise RuntimeError(f"Configured FX rate {pair} must be positive; got {rate}")
        return rate

    @staticmethod
    def _today() -> date:
        return date.today()

    def _request_fx_rate(self, pair: str) -> float | None:
        contract = Forex(pair)
        try:
            qualified = self._ib.qualifyContracts(contract)
            if not qualified:
                logger.warning("ibkr_fx_contract_not_qualified", pair=pair)
                return None
            contract = qualified[0]
            ticker = self._ib.reqMktData(contract, "", False, False)
        except Exception as exc:
            logger.warning("ibkr_fx_request_failed", pair=pair, error=str(exc))
            return None

        deadline = time.time() + _FX_RATE_TIMEOUT_SECONDS
        try:
            while time.time() <= deadline:
                rate = self._ticker_fx_rate(ticker)
                if rate is not None:
                    return rate
                self._ib.sleep(_FX_RATE_POLL_INTERVAL)
        finally:
            self._ib.cancelMktData(contract)
        return None

    @staticmethod
    def _ticker_fx_rate(ticker: object) -> float | None:
        market_price = getattr(ticker, "marketPrice", lambda: None)()
        if market_price and market_price > 0:
            return float(market_price)

        bid = getattr(ticker, "bid", None)
        ask = getattr(ticker, "ask", None)
        if bid and ask and bid > 0 and ask > 0:
            return float((bid + ask) / 2.0)
        if bid and bid > 0:
            return float(bid)
        if ask and ask > 0:
            return float(ask)
        return None

    @staticmethod
    def _normalize_currency(currency: str | None) -> str:
        return (currency or "").strip().upper()

    @classmethod
    def _validate_currency_code(cls, currency: str | None, label: str) -> str:
        normalized = cls._normalize_currency(currency)
        if len(normalized) != 3 or not normalized.isalpha():
            raise RuntimeError(f"IBKR {label} must be a 3-letter currency code; got {currency!r}")
        return normalized

    def _require_connection(self) -> None:
        if self._ib is None or not self._ib.isConnected():
            raise RuntimeError(
                "Not connected to IBKR. Call connect() first."
            )
