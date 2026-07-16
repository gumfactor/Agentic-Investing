"""Interactive Brokers broker implementation using ib_insync.

Paper trading:  IBKR_PORT=7497  (TWS Paper or IB Gateway Paper)
Live trading:   IBKR_PORT=7496  (TWS Live or IB Gateway Live)

Safety rules enforced here:
- C1: order submission blocked unless caller has already obtained "YES"
      (enforced by OrderManager, not here - this class blindly submits)
- C8: raises EnvironmentError if PAPER_TRADING=false and the 4-week automated
      paper-trading qualification has not been cleared (checked via env var
      PAPER_RUN_CLEARED)
- C9: the live vs. paper switch is governed entirely by IBKR_PORT +
      PAPER_TRADING env vars; never hardcoded.
- BUG-004: in a bridged Docker Compose runtime (RQIS_RUNTIME_CONTEXT=
      compose_bridged), an unset/empty/loopback IBKR_HOST is a configuration
      error and is rejected before a connection is attempted - "127.0.0.1"
      inside a bridged container resolves to the container itself, not the
      Docker host running TWS/IB Gateway. See _validate_bridged_broker_host().
"""

from __future__ import annotations

import math
import os
import time
from datetime import date

import structlog

from execution.brokers.base import BaseBroker
from execution.oms.order import Order, OrderSide

logger = structlog.get_logger(__name__)

try:
    from ib_insync import IB, Forex, LimitOrder, MarketOrder, Stock

    _IB_AVAILABLE = True
except ImportError:
    _IB_AVAILABLE = False
    logger.warning("ib_insync_not_installed", advice="pip install ib-insync")

_ORDER_ID_TIMEOUT_SECONDS = 5.0
_ORDER_ID_POLL_INTERVAL = 0.05
_FX_RATE_TIMEOUT_SECONDS = 5.0
_FX_RATE_POLL_INTERVAL = 0.1
_CONFIGURED_FX_RATE_MAX_AGE_DAYS = 1

# BUG-004: hosts that only resolve inside the calling process's own network
# namespace. Legitimate when a script runs directly on the same host as
# TWS/IB Gateway; unsafe when the caller is a container on a bridged Docker
# Compose network, because "127.0.0.1"/"localhost" then means the container
# itself rather than the Docker host.
_LOOPBACK_BROKER_HOSTS = frozenset({"", "127.0.0.1", "localhost", "::1", "0.0.0.0"})

# Set on every Airflow Compose service (see docker-compose.yml x-airflow-common)
# so this module can distinguish "running inside a bridged container" from
# "running as a host-side script" without guessing from the network stack.
_BRIDGED_RUNTIME_CONTEXT = "compose_bridged"


def _validate_bridged_broker_host(host: str | None) -> None:
    """Fail closed (BUG-004) if IBKR_HOST is a loopback value inside a bridged
    Docker Compose runtime.

    This check is a no-op only when RQIS_RUNTIME_CONTEXT is entirely unset or
    empty (host-side operator CLI scripts, where 127.0.0.1 is the correct
    address for TWS/IB Gateway running on the same machine). ANY non-empty
    value arms the guard: "compose_bridged" is the reviewed value set on
    every Airflow Compose service, and any other non-empty value (a typo
    like "compose-bridged", an unreviewed future deployment label) is
    treated fail-closed as containerized rather than silently deactivating
    enforcement (adversarial fix round P2-2).

    The loopback exception is granted only when RQIS_RUNTIME_NETWORK_MODE=host
    is also explicitly set, declaring (and presumably tested against) Docker
    host networking rather than the default bridge network.
    """
    runtime_context = os.environ.get("RQIS_RUNTIME_CONTEXT", "").strip().lower()
    if not runtime_context:
        return

    network_mode = os.environ.get("RQIS_RUNTIME_NETWORK_MODE", "").strip().lower()
    if network_mode == "host":
        return

    normalized = (host or "").strip().lower()
    if normalized in _LOOPBACK_BROKER_HOSTS:
        context_note = (
            "RQIS_RUNTIME_CONTEXT=compose_bridged"
            if runtime_context == _BRIDGED_RUNTIME_CONTEXT
            else (
                f"RQIS_RUNTIME_CONTEXT={runtime_context!r} is an unrecognized "
                "non-empty runtime context, enforced fail-closed as containerized"
            )
        )
        raise OSError(
            f"IBKR_HOST={host!r} is not reachable from a containerized runtime "
            f"({context_note}): a loopback address resolves to the container "
            "itself, not the Docker host running TWS/IB Gateway (BUG-004). Set "
            "IBKR_HOST to 'host.docker.internal' on Windows/Mac Docker Desktop, "
            "or an explicit gateway address on Linux Docker Engine. If this "
            "container deliberately uses Docker host networking, set "
            "RQIS_RUNTIME_NETWORK_MODE=host to declare and test that exception "
            "explicitly."
        )


def _client_id_from_env() -> int:
    """Resolve the default IBKR client id from IBKR_CLIENT_ID (BUG-001/P1-2).

    docker-compose.yml passes IBKR_CLIENT_ID into every Airflow service, and
    the DAG constructs IBKRBroker() bare -- so the env var must actually be
    consumed here, not just declared. Falls back to 1 only when the variable
    is unset or empty; a set-but-invalid value (non-integer, zero/negative)
    is a configuration error and fails closed rather than silently becoming 1.
    """
    raw = os.environ.get("IBKR_CLIENT_ID", "").strip()
    if not raw:
        return 1
    try:
        value = int(raw)
    except ValueError:
        raise OSError(
            f"IBKR_CLIENT_ID={raw!r} is not a valid integer. Set it to a "
            "positive integer (each concurrent IBKR API session needs a "
            "distinct client id) or unset it to use the default of 1."
        ) from None
    if value < 0:
        raise OSError(
            f"IBKR_CLIENT_ID={value} must be a non-negative integer "
            "(IBKR client ids are >= 0)."
        )
    return value


class IBKRBroker(BaseBroker):
    """IBKR TWS/Gateway broker via ib_insync.

    Parameters
    ----------
    host:
        IB Gateway / TWS host.  Defaults to IBKR_HOST env var or 127.0.0.1.
    port:
        7497 = paper, 7496 = live.  Defaults to IBKR_PORT env var.
    client_id:
        Unique client ID for this connection. Defaults to the IBKR_CLIENT_ID
        env var when set (validated integer), else 1.
    timeout:
        Connection timeout in seconds.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        timeout: int = 10,
    ) -> None:
        if not _IB_AVAILABLE:
            raise ImportError("ib_insync is required. `pip install ib-insync`")

        self._host = host or os.environ.get("IBKR_HOST", "127.0.0.1")
        raw_port = port or int(os.environ.get("IBKR_PORT", "7497"))
        self._port = raw_port
        self._client_id = client_id if client_id is not None else _client_id_from_env()
        self._timeout = timeout
        self._ib: IB | None = None
        self._submitted: dict[str, object] = {}  # broker_order_id -> ib Trade

        _validate_bridged_broker_host(self._host)
        self._validate_paper_trading_flag()

    def _validate_paper_trading_flag(self) -> None:
        """Enforce C8/C9: live trading gate requires explicit opt-in + paper qualification.

        Rules:
        - Port 7496 (live) requires PAPER_TRADING=false explicitly set.
          An unset PAPER_TRADING env var defaults to paper mode even if port=7496
          was passed - prevents accidental live connection (C9).
        - PAPER_TRADING=false requires PAPER_RUN_CLEARED=true (C8).
        - PAPER_TRADING=false with port != 7496 is a configuration error (C9).
        """
        paper_env = os.environ.get("PAPER_TRADING", "true").lower()

        # Block live port unless PAPER_TRADING is explicitly disabled
        if self._port == 7496 and paper_env != "false":
            raise OSError(
                f"IBKR_PORT={self._port} (live) requires PAPER_TRADING=false to be explicitly set. "
                "Default/unset PAPER_TRADING is treated as paper mode (C9)."
            )
        if paper_env == "false" and self._port != 7496:
            raise OSError(
                "PAPER_TRADING=false but IBKR_PORT is not 7496.  "
                "Set IBKR_PORT=7496 for live trading (C9)."
            )
        if paper_env == "false":
            cleared = os.environ.get("PAPER_RUN_CLEARED", "false").lower()
            if cleared != "true":
                raise OSError(
                    "Live trading requires PAPER_RUN_CLEARED=true.  "
                    "Confirm 4 consecutive weeks of clean automated paper trading before switching (C8)."
                )

    # Connection

    def connect(self) -> None:
        # Re-validate env vars at connection time; they may have changed since __init__
        _validate_bridged_broker_host(self._host)
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

    # Order submission

    def submit_order(self, order: Order) -> str:
        self._require_connection()
        contract = Stock(order.ticker, "SMART", "USD")
        action = "BUY" if order.side == OrderSide.BUY else "SELL"

        if order.limit_price is not None:
            ib_order = LimitOrder(action, order.quantity, order.limit_price, tif="DAY")
        else:
            ib_order = MarketOrder(action, order.quantity, tif="DAY")

        trade = self._ib.placeOrder(contract, ib_order)

        # Poll the event loop until TWS assigns a real orderId (0 = not yet assigned).
        # Using ib.sleep() pumps the asyncio event loop so the callback can fire.
        deadline = time.time() + _ORDER_ID_TIMEOUT_SECONDS
        while trade.order.orderId == 0:
            if time.time() > deadline:
                raise RuntimeError(
                    f"IBKR did not assign an orderId within {_ORDER_ID_TIMEOUT_SECONDS}s "
                    f"for {order.ticker} {action} {order.quantity}. "
                    "Order may still be live at broker - check TWS before retrying."
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

    def what_if_order(self, order: Order) -> dict:
        """Ask IBKR to validate an order without transmitting it."""
        self._require_connection()
        contract = Stock(order.ticker, "SMART", "USD")
        action = "BUY" if order.side == OrderSide.BUY else "SELL"

        if order.limit_price is not None:
            ib_order = LimitOrder(action, order.quantity, order.limit_price, tif="DAY")
        else:
            ib_order = MarketOrder(action, order.quantity, tif="DAY")

        errors: list[str] = []

        def record_error(req_id, error_code, error_string, error_contract=None) -> None:
            errors.append(f"IBKR error {error_code}: {error_string}")

        self._ib.errorEvent += record_error
        try:
            order_state = self._ib.whatIfOrder(contract, ib_order)
        finally:
            self._ib.errorEvent -= record_error

        if errors:
            raise RuntimeError("; ".join(errors))
        result = {
            "status": getattr(order_state, "status", ""),
            "init_margin_before": getattr(order_state, "initMarginBefore", ""),
            "init_margin_change": getattr(order_state, "initMarginChange", ""),
            "init_margin_after": getattr(order_state, "initMarginAfter", ""),
            "maint_margin_before": getattr(order_state, "maintMarginBefore", ""),
            "maint_margin_change": getattr(order_state, "maintMarginChange", ""),
            "maint_margin_after": getattr(order_state, "maintMarginAfter", ""),
            "equity_with_loan_before": getattr(order_state, "equityWithLoanBefore", ""),
            "equity_with_loan_change": getattr(order_state, "equityWithLoanChange", ""),
            "equity_with_loan_after": getattr(order_state, "equityWithLoanAfter", ""),
            "commission": getattr(order_state, "commission", ""),
            "min_commission": getattr(order_state, "minCommission", ""),
            "max_commission": getattr(order_state, "maxCommission", ""),
            "commission_currency": getattr(order_state, "commissionCurrency", ""),
            "warning_text": getattr(order_state, "warningText", ""),
            "completed_status": getattr(order_state, "completedStatus", ""),
        }
        if not any(str(value).strip() for value in result.values()):
            raise RuntimeError("IBKR what-if returned an empty order state")
        logger.info(
            "ibkr_what_if_order",
            ticker=order.ticker,
            side=action,
            quantity=order.quantity,
            limit=order.limit_price,
            status=result["status"],
            warning=result["warning_text"],
        )
        return result

    # Fill polling

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

    def get_order_status(self, broker_order_id: str) -> dict | None:
        """Read current broker order/fill state by broker order id."""
        self._require_connection()
        self._ib.sleep(0)

        target_id = str(broker_order_id)
        trades = list(self._submitted.values())
        for method_name in ("trades", "openTrades"):
            method = getattr(self._ib, method_name, None)
            if method is not None:
                try:
                    trades.extend(method())
                except Exception as exc:
                    logger.warning("ibkr_order_status_trade_query_failed", method=method_name, error=str(exc))

        trade = self._find_trade_by_order_id(trades, target_id)
        if trade is not None:
            return self._order_status_from_trade(trade)

        req_open_orders = getattr(self._ib, "reqOpenOrders", None)
        if req_open_orders is not None:
            req_open_orders()
            self._ib.sleep(0)
            for method_name in ("trades", "openTrades"):
                method = getattr(self._ib, method_name, None)
                if method is not None:
                    trade = self._find_trade_by_order_id(method(), target_id)
                    if trade is not None:
                        return self._order_status_from_trade(trade)

        completed_orders = self._completed_orders()
        completed_trade = self._find_trade_by_order_id(completed_orders, target_id)
        if completed_trade is not None:
            status = self._order_status_from_trade(completed_trade)
            status["status"] = status["status"] or "Completed"
            return status
        completed_order = self._find_order_by_order_id(completed_orders, target_id)
        if completed_order is not None:
            return {"broker_order_id": target_id, "status": "Completed", "filled_quantity": None, "avg_price": None}
        return None

    # Order cancellation

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

    # Account state

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

    def get_cash_balance_usd(self) -> float:
        """Return total cash balance across all currencies, converted to USD."""
        values = self._account_values_for_tag("TotalCashValue", "$LEDGER-CashBalance")
        if not values:
            raise RuntimeError(
                "TotalCashValue not found in IBKR account values for any currency. "
                "Account data may not have arrived yet; retry after a short delay."
            )
        total = 0.0
        for source_currency, value in values.items():
            if source_currency == "USD":
                total += value
            else:
                total += value * self._get_fx_rate(source_currency, "USD")
        return total

    def _account_values_for_tag(self, tag: str, ledger_tag: str) -> dict[str, float]:
        """Return {currency: value} for an account value tag, preferring per-currency ledger."""
        self._ib.sleep(0)
        simple: dict[str, float] = {}
        ledger: dict[str, float] = {}
        for av in self._ib.accountValues():
            cur = self._normalize_currency(av.currency)
            if not cur or cur == "BASE":
                continue
            if av.tag == tag:
                simple[cur] = float(av.value)
            elif av.tag == ledger_tag:
                ledger[cur] = float(av.value)
        if len(ledger) > 1:
            return ledger
        if simple:
            return simple
        if ledger:
            return ledger
        return {}

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
        if not math.isfinite(rate) or rate <= 0:
            raise RuntimeError(f"Configured FX rate {pair} must be finite and positive; got {rate}")
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
    def _order_id(value: object) -> str | None:
        order_id = getattr(value, "orderId", None)
        if order_id in {None, ""}:
            return None
        return str(order_id)

    @classmethod
    def _find_trade_by_order_id(cls, trades: list[object], broker_order_id: str) -> object | None:
        for trade in trades:
            order = getattr(trade, "order", None)
            if order is not None and cls._order_id(order) == broker_order_id:
                return trade
        return None

    @classmethod
    def _find_order_by_order_id(cls, orders: list[object], broker_order_id: str) -> object | None:
        for order in orders:
            if cls._order_id(order) == broker_order_id:
                return order
        return None

    @staticmethod
    def _order_status_from_trade(trade: object) -> dict:
        order = getattr(trade, "order", None)
        status = getattr(trade, "orderStatus", None)
        return {
            "broker_order_id": str(getattr(order, "orderId", "")),
            "status": getattr(status, "status", None),
            "filled_quantity": getattr(status, "filled", None),
            "remaining_quantity": getattr(status, "remaining", None),
            "avg_price": getattr(status, "avgFillPrice", None),
            "last_fill_price": getattr(status, "lastFillPrice", None),
            "why_held": getattr(status, "whyHeld", None),
        }

    def _completed_orders(self) -> list[object]:
        completed_orders = getattr(self._ib, "completedOrders", None)
        if completed_orders is not None:
            if not callable(completed_orders):
                return list(completed_orders)
            try:
                return list(completed_orders())
            except Exception as exc:
                logger.warning("ibkr_completed_orders_query_failed", error=str(exc))

        req_completed_orders = getattr(self._ib, "reqCompletedOrders", None)
        if req_completed_orders is not None:
            result = req_completed_orders(apiOnly=False)
            self._ib.sleep(0)
            return [] if result is None else list(result)
        return []

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
            raise RuntimeError("Not connected to IBKR. Call connect() first.")
