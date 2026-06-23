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
    from execution.oms.trade_history import TradeJournal
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
        trade_journal: "TradeJournal | None" = None,
    ) -> None:
        self._compliance = compliance or ComplianceEngine()
        self._broker = broker
        self._circuit_breaker = circuit_breaker
        self._trade_journal = trade_journal
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
            context = {**context, "circuit_breaker_open": True}
            logger.error(
                "compliance_context_missing_circuit_breaker",
                advice="Pass circuit_breaker_open=False explicitly or provide a CircuitBreaker to OrderManager. "
                       "Defaulting to circuit_breaker_open=True — ALL orders will be rejected this run.",
            )

        staged = [o for o in self._orders.values() if o.status == OrderStatus.STAGED]

        # Auto-inject wash-sale context from trade journal if available and not
        # already provided by caller.  Populates ctx['recent_loss_buys'] so the
        # previously-stubbed _check_wash_sale compliance check fires correctly.
        if self._trade_journal is not None and "recent_loss_buys" not in context:
            sell_tickers = [o.ticker for o in staged if o.side == OrderSide.SELL]
            if sell_tickers:
                as_of = context.get("as_of_date")
                try:
                    context = {
                        **context,
                        "recent_loss_buys": self._trade_journal.wash_sale_context(
                            sell_tickers, as_of=as_of
                        ),
                    }
                except Exception as exc:
                    logger.error(
                        "wash_sale_context_failed",
                        error=str(exc),
                        advice="Defaulting to empty wash-sale context; check trade journal DB connection.",
                    )
        approved: list[Order] = []
        rejected: list[Order] = []

        for order in staged:
            passed, reason = self._compliance.check(order, context)
            if passed:
                order.transition(OrderStatus.PENDING)
                approved.append(order)
                # Update the simulated context weights after each approval so that
                # subsequent orders in the same batch see the cumulative post-trade exposure.
                # Without this, two BUY orders each adding 3% to a sector at 20% both pass
                # a 25% cap — neither sees the other's 3% delta.
                total_nav = context.get("total_nav", 0.0)
                if order.limit_price and total_nav > 0:
                    import pandas as pd
                    trade_w = (order.quantity * order.limit_price) / total_nav
                    cur_w = context.get("current_weights", pd.Series(dtype=float)).copy()
                    if order.side == OrderSide.BUY:
                        cur_w[order.ticker] = float(cur_w.get(order.ticker, 0.0)) + trade_w
                    else:
                        cur_w[order.ticker] = max(0.0, float(cur_w.get(order.ticker, 0.0)) - trade_w)
                    context = {**context, "current_weights": cur_w}
                    # Invalidate derived sector_weights so _check_sector_concentration
                    # re-derives from the updated current_weights on the next order.
                    context.pop("sector_weights", None)
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

        # Double-submission guard: reject any PENDING order whose (ticker, side) already has a
        # SUBMITTED order.  Uses (ticker, side) — not ticker alone — so a SELL is not blocked
        # by a pre-existing SUBMITTED BUY for the same ticker (a legitimate rebalance pattern).
        submitted_keys: set[tuple[str, str]] = {
            (o.ticker, o.side.value) for o in self._orders.values() if o.status == OrderStatus.SUBMITTED
        }
        for order in pending:
            key = (order.ticker, order.side.value)
            if key in submitted_keys:
                logger.error(
                    "double_submission_risk",
                    ticker=order.ticker,
                    side=order.side.value,
                    order_id=order.order_id[:8],
                    advice="A SUBMITTED order for this (ticker, side) already exists. "
                           "Skipping to prevent duplicate order at broker.",
                )
                order.transition(OrderStatus.REJECTED, reason="double_submission_prevented")
        pending = [o for o in pending if o.status == OrderStatus.PENDING]

        submitted: list[Order] = []

        for order in pending:
            # Re-check circuit breaker before each order (breach may fire mid-batch)
            if self._circuit_breaker is not None and self._circuit_breaker.is_open:
                order.transition(OrderStatus.REJECTED, reason="circuit_breaker_opened_mid_batch")
                logger.error("circuit_breaker_opened_mid_batch", order_id=order.order_id[:8])
                continue
            # Within-batch duplicate guard: catches two PENDING orders for the same
            # (ticker, side) in the same batch (e.g. portfolio construction bug).
            key = (order.ticker, order.side.value)
            if key in submitted_keys:
                logger.error(
                    "double_submission_risk_within_batch",
                    ticker=order.ticker,
                    side=order.side.value,
                    order_id=order.order_id[:8],
                    advice="A second PENDING order for the same (ticker, side) found in this batch. "
                           "Skipping to prevent duplicate order at broker.",
                )
                order.transition(OrderStatus.REJECTED, reason="double_submission_prevented")
                continue
            try:
                broker_id = self._broker.submit_order(order)
                order.broker_order_id = broker_id
                order.transition(OrderStatus.SUBMITTED)
                submitted.append(order)
                submitted_keys.add(key)
                logger.info(
                    "order_submitted",
                    order_id=order.order_id[:8],
                    broker_order_id=broker_id,
                    ticker=order.ticker,
                    side=order.side.value,
                    quantity=order.quantity,
                )
            except (ConnectionError, TimeoutError, OSError) as exc:
                # Transient network error — leave order in PENDING so it can be retried.
                logger.error(
                    "order_submission_network_error",
                    order_id=order.order_id[:8],
                    ticker=order.ticker,
                    error=str(exc),
                    advice="Order left in PENDING status. Retry submit_pending() after connection recovers.",
                )
            except Exception as exc:
                # Permanent broker rejection (margin violation, unknown symbol, etc.)
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
        """Poll broker for fill status on all SUBMITTED and PARTIALLY_FILLED orders.

        Returns orders that transitioned to FILLED this cycle.  Orders with
        a partial fill are transitioned to PARTIALLY_FILLED and remain active
        for subsequent reconciliation cycles.
        """
        if self._broker is None:
            raise RuntimeError("No broker configured.")

        active = [
            o for o in self._orders.values()
            if o.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}
        ]
        newly_filled: list[Order] = []

        for order in active:
            if order.broker_order_id is None:
                logger.error(
                    "reconcile_skipped_missing_broker_id",
                    order_id=order.order_id[:8],
                    ticker=order.ticker,
                    status=order.status.value,
                    advice="Order is stuck — no broker_order_id was recorded at submission.",
                )
                continue
            fill = self._broker.get_fill(order.broker_order_id)
            if fill is None:
                continue
            new_qty = fill["filled_quantity"]
            if new_qty < order.filled_quantity - 1e-9:
                logger.error(
                    "fill_went_backwards",
                    order_id=order.order_id[:8],
                    ticker=order.ticker,
                    previous_qty=order.filled_quantity,
                    reported_qty=new_qty,
                )
                continue  # do not overwrite; preserve prior fill record
            order.filled_quantity = new_qty
            order.avg_fill_price = fill["avg_price"]

            if fill["filled_quantity"] >= order.quantity - 1e-6:
                # Full fill
                order.transition(OrderStatus.FILLED)
                newly_filled.append(order)
                self._record_fill_to_journal(order)
                logger.info(
                    "order_filled",
                    order_id=order.order_id[:8],
                    ticker=order.ticker,
                    filled_qty=fill["filled_quantity"],
                    avg_price=fill["avg_price"],
                )
            else:
                # Partial fill — stay active, re-polled next cycle
                order.transition(OrderStatus.PARTIALLY_FILLED)
                self._record_fill_to_journal(order)
                logger.warning(
                    "order_partially_filled",
                    order_id=order.order_id[:8],
                    ticker=order.ticker,
                    filled_qty=fill["filled_quantity"],
                    ordered_qty=order.quantity,
                    pct_filled=round(fill["filled_quantity"] / order.quantity, 3),
                )

        return newly_filled

    def _record_fill_to_journal(self, order: Order) -> None:
        """Attempt to persist a fill to the trade journal; log errors, never raise.

        Exceptions are swallowed so that a journal persistence failure does not
        abort the reconciliation loop or leave in-memory OMS state inconsistent.
        If this method logs an error, the OMS order will be in FILLED state but
        the journal will be missing the fill.  To recover, call:

            journal.recover_missed_fills(list(order_manager.all_orders()))

        This replays fills for FILLED orders not yet recorded in the journal and
        is safe to call multiple times (already-recorded fills are skipped).
        """
        if self._trade_journal is None:
            return
        try:
            self._trade_journal.record_fill(order)
        except ValueError as exc:
            # Dedup guard or long-only violation — both indicate a programming
            # error upstream; log at error level so they're not silently ignored.
            logger.error(
                "trade_journal_record_fill_rejected",
                order_id=order.order_id[:8],
                ticker=order.ticker,
                error=str(exc),
                recovery="call journal.recover_missed_fills(om.all_orders())",
            )
        except Exception as exc:
            logger.error(
                "trade_journal_record_fill_failed",
                order_id=order.order_id[:8],
                ticker=order.ticker,
                error=str(exc),
                recovery="call journal.recover_missed_fills(om.all_orders()) once DB is back",
            )

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
