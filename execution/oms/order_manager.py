"""Order Management System — central coordinator.

The OrderManager:
1. Accepts staged orders from portfolio construction.
2. Runs pre-trade compliance checks via ComplianceEngine.
3. Displays pending orders to the operator for C1 confirmation.
4. Submits approved orders to the broker.
5. Reconciles fills back into portfolio state.

Safety rule C1 is enforced here: no order reaches SUBMITTED without the
operator having typed "YES" (checked by the caller or skill layer).
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

from execution.oms.compliance import ComplianceEngine
from execution.oms.order import Order, OrderSide, OrderStatus

if TYPE_CHECKING:
    from execution.brokers.base import BaseBroker
    from risk.circuit_breaker import CircuitBreaker

logger = structlog.get_logger(__name__)


class OrderManager:
    """Coordinates order lifecycle from STAGED to FILLED.

    Parameters
    ----------
    compliance:
        ComplianceEngine instance.  Defaults to the standard check suite.
    broker:
        Broker implementation.  Must be set before calling submit_pending().
    circuit_breaker:
        Optional CircuitBreaker instance.  When provided, run_compliance()
        automatically injects `circuit_breaker_open` into the context dict
        and submit_pending() re-checks the breaker state immediately before
        any order is sent to the broker.
    """

    def __init__(
        self,
        compliance: ComplianceEngine | None = None,
        broker: "BaseBroker | None" = None,
        circuit_breaker: "CircuitBreaker | None" = None,
    ) -> None:
        self._compliance = compliance or ComplianceEngine()
        self._broker = broker
        self._circuit_breaker = circuit_breaker
        self._orders: dict[str, Order] = {}

    # ── Staging ──────────────────────────────────────────────────────────────

    def stage(self, order: Order) -> str:
        """Accept an order into the OMS at STAGED status.

        Returns the order_id.
        """
        if order.status != OrderStatus.STAGED:
            raise ValueError(f"Only STAGED orders may be added; got {order.status}")
        self._orders[order.order_id] = order
        logger.info("order_staged", order_id=order.order_id[:8], ticker=order.ticker, side=order.side.value)
        return order.order_id

    def stage_batch(self, orders: list[Order]) -> list[str]:
        """Stage a list of orders; returns list of order_ids."""
        return [self.stage(o) for o in orders]

    # ── Compliance ───────────────────────────────────────────────────────────

    def run_compliance(self, context: dict) -> tuple[list[Order], list[Order]]:
        """Run compliance checks on all STAGED orders.

        Returns (approved, rejected) lists.  Approved orders are transitioned
        to PENDING; rejected to REJECTED.

        If a CircuitBreaker was provided at construction, its current state is
        automatically injected into the context (callers need not set it manually).
        """
        # Auto-inject circuit breaker state so compliance check is never skipped
        if self._circuit_breaker is not None:
            context = {**context, "circuit_breaker_open": self._circuit_breaker.is_open}
        elif "circuit_breaker_open" not in context:
            # Safe default: if no CB and no explicit key, treat as open (block orders).
            # Callers must explicitly pass circuit_breaker_open=False to allow through.
            logger.warning(
                "compliance_context_missing_circuit_breaker",
                advice="Pass circuit_breaker_open=False explicitly or provide a CircuitBreaker to OrderManager.",
            )

        staged = [o for o in self._orders.values() if o.status == OrderStatus.STAGED]
        approved: list[Order] = []
        rejected: list[Order] = []

        for order, passed, reason in self._compliance.check_batch(staged, context):
            if passed:
                order.transition(OrderStatus.PENDING)
                approved.append(order)
            else:
                order.transition(OrderStatus.REJECTED, reason=reason)
                rejected.append(order)

        logger.info(
            "compliance_run",
            checked=len(staged),
            approved=len(approved),
            rejected=len(rejected),
        )
        return approved, rejected

    # ── Operator display (C1) ─────────────────────────────────────────────────

    def pending_orders_display(self) -> list[dict]:
        """Return display rows for all PENDING orders.

        The skill / CLI layer must show this to the operator and require "YES"
        before calling submit_pending().  See safety rule C1.
        """
        pending = [o for o in self._orders.values() if o.status == OrderStatus.PENDING]
        return [o.to_display_row() for o in pending]

    # ── Submission ────────────────────────────────────────────────────────────

    def submit_pending(self) -> list[Order]:
        """Submit all PENDING orders to the broker.

        IMPORTANT: The caller MUST have received explicit "YES" confirmation
        from the operator before calling this method (safety rule C1).

        Raises
        ------
        RuntimeError if no broker is configured.
        RuntimeError if the circuit breaker is open (C4 — prevents TOCTOU gap
            between run_compliance() and submission).
        """
        if self._broker is None:
            raise RuntimeError("No broker configured. Set OrderManager._broker before submitting.")

        # Re-check circuit breaker immediately before submission (TOCTOU fix).
        # A breach may have fired between run_compliance() and this call.
        if self._circuit_breaker is not None and self._circuit_breaker.is_open:
            raise RuntimeError(
                "Circuit breaker is OPEN. Order submission blocked (C4). "
                "A human operator must reset it with circuit_breaker.reset(operator, reason_code)."
            )

        pending = [o for o in self._orders.values() if o.status == OrderStatus.PENDING]
        submitted: list[Order] = []

        for order in pending:
            try:
                broker_id = self._broker.submit_order(order)
                order.broker_order_id = broker_id
                order.transition(OrderStatus.SUBMITTED)
                submitted.append(order)
                logger.info(
                    "order_submitted",
                    order_id=order.order_id[:8],
                    broker_order_id=broker_id,
                    ticker=order.ticker,
                    side=order.side.value,
                    quantity=order.quantity,
                )
            except Exception as exc:
                order.transition(OrderStatus.REJECTED, reason=str(exc))
                logger.error(
                    "order_submission_failed",
                    order_id=order.order_id[:8],
                    ticker=order.ticker,
                    error=str(exc),
                )

        return submitted

    # ── Fill reconciliation ───────────────────────────────────────────────────

    def reconcile_fills(self) -> list[Order]:
        """Poll broker for fill status on all SUBMITTED orders.

        Returns orders that transitioned to FILLED this cycle.
        """
        if self._broker is None:
            raise RuntimeError("No broker configured.")

        submitted = [o for o in self._orders.values() if o.status == OrderStatus.SUBMITTED]
        newly_filled: list[Order] = []

        for order in submitted:
            if order.broker_order_id is None:
                continue
            fill = self._broker.get_fill(order.broker_order_id)
            if fill is None:
                continue
            order.filled_quantity = fill["filled_quantity"]
            order.avg_fill_price = fill["avg_price"]
            order.transition(OrderStatus.FILLED)
            newly_filled.append(order)
            logger.info(
                "order_filled",
                order_id=order.order_id[:8],
                ticker=order.ticker,
                filled_qty=fill["filled_quantity"],
                avg_price=fill["avg_price"],
            )

        return newly_filled

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a STAGED or PENDING order.  Returns True if cancelled."""
        order = self._orders.get(order_id)
        if order is None:
            return False
        if order.status in {OrderStatus.STAGED, OrderStatus.PENDING}:
            order.transition(OrderStatus.CANCELLED)
            logger.info("order_cancelled", order_id=order_id[:8])
            return True
        return False

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())

    def pending_count(self) -> int:
        return sum(1 for o in self._orders.values() if o.status == OrderStatus.PENDING)
