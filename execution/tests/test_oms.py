"""Tests for the OMS state machine, compliance, and order manager."""

from __future__ import annotations

from datetime import date

import pytest

from execution.oms.compliance import ComplianceEngine
from execution.oms.order import Order, OrderSide, OrderStatus
from execution.oms.order_manager import OrderManager


# ── Order state machine ───────────────────────────────────────────────────────

class TestOrder:
    def test_default_status_is_staged(self):
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100)
        assert o.status == OrderStatus.STAGED

    def test_valid_transition_staged_to_pending(self):
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100)
        o.transition(OrderStatus.PENDING)
        assert o.status == OrderStatus.PENDING

    def test_invalid_transition_raises(self):
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100)
        with pytest.raises(ValueError, match="Invalid transition"):
            o.transition(OrderStatus.FILLED)

    def test_rejected_records_reason(self):
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100)
        o.transition(OrderStatus.REJECTED, reason="test reason")
        assert o.rejection_reason == "test reason"

    def test_terminal_states(self):
        for status in [OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED]:
            o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100, status=status)
            assert o.is_terminal

    def test_to_display_row_has_required_keys(self):
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100, limit_price=150.0)
        row = o.to_display_row()
        for key in ["ticker", "side", "quantity", "status"]:
            assert key in row


# ── Compliance ────────────────────────────────────────────────────────────────

class TestComplianceEngine:
    def _order(self, ticker="AAPL", side=OrderSide.BUY, qty=100, price=150.0):
        return Order(ticker=ticker, side=side, quantity=qty, limit_price=price)

    def test_circuit_breaker_blocks_all_orders(self):
        engine = ComplianceEngine()
        ctx = {"circuit_breaker_open": True}
        o = self._order()
        passed, reason = engine.check(o, ctx)
        assert not passed
        assert "circuit_breaker" in reason

    def test_concentration_rejected(self):
        import pandas as pd
        engine = ComplianceEngine()
        ctx = {
            "max_position_weight": 0.05,
            "current_weights": pd.Series({"AAPL": 0.049}),
            "total_nav": 1_000_000.0,
            "min_order_notional": 0.0,
        }
        # Trade would push AAPL to ~6.4%
        o = self._order(qty=85, price=150.0)  # notional = 12,750 = 1.275% of 1M
        # current 4.9% + 1.275% = 6.175% > 5%
        passed, reason = engine.check(o, ctx)
        assert not passed
        assert "concentration" in reason

    def test_wash_sale_blocks_sell(self):
        engine = ComplianceEngine()
        ctx = {
            "recent_loss_buys": {"AAPL": date(2024, 1, 1)},
            "as_of_date": date(2024, 1, 15),
        }
        o = self._order(side=OrderSide.SELL)
        passed, reason = engine.check(o, ctx)
        assert not passed
        assert "wash-sale" in reason

    def test_wash_sale_allows_buy(self):
        engine = ComplianceEngine()
        ctx = {
            "recent_loss_buys": {"AAPL": date(2024, 1, 1)},
            "as_of_date": date(2024, 1, 15),
        }
        o = self._order(side=OrderSide.BUY)
        passed, _ = engine.check(o, ctx)
        assert passed

    def test_min_notional_rejected(self):
        engine = ComplianceEngine()
        ctx = {"min_order_notional": 1000.0}
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=1, limit_price=50.0)
        passed, reason = engine.check(o, ctx)
        assert not passed
        assert "below_min_notional" in reason

    def test_clean_order_passes(self):
        engine = ComplianceEngine()
        ctx = {"min_order_notional": 0.0}
        o = self._order()
        passed, reason = engine.check(o, ctx)
        assert passed
        assert reason == ""

    def test_check_batch(self):
        engine = ComplianceEngine()
        ctx = {"circuit_breaker_open": True}
        orders = [self._order() for _ in range(3)]
        results = engine.check_batch(orders, ctx)
        assert all(not passed for _, passed, _ in results)


# ── OrderManager ─────────────────────────────────────────────────────────────

class TestOrderManager:
    def test_stage_and_retrieve(self):
        om = OrderManager()
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100)
        oid = om.stage(o)
        assert om.get_order(oid) is o

    def test_compliance_approves_clean_orders(self):
        om = OrderManager()
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100, limit_price=150.0)
        om.stage(o)
        ctx = {"min_order_notional": 0.0}
        approved, rejected = om.run_compliance(ctx)
        assert len(approved) == 1
        assert len(rejected) == 0
        assert o.status == OrderStatus.PENDING

    def test_compliance_rejects_circuit_breaker(self):
        om = OrderManager()
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100)
        om.stage(o)
        approved, rejected = om.run_compliance({"circuit_breaker_open": True})
        assert len(rejected) == 1
        assert o.status == OrderStatus.REJECTED

    def test_pending_orders_display(self):
        om = OrderManager()
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100, limit_price=150.0)
        om.stage(o)
        om.run_compliance({"min_order_notional": 0.0})
        rows = om.pending_orders_display()
        assert len(rows) == 1
        assert rows[0]["ticker"] == "AAPL"

    def test_submit_without_broker_raises(self):
        om = OrderManager()
        with pytest.raises(RuntimeError, match="No broker"):
            om.submit_pending()

    def test_cancel_staged_order(self):
        om = OrderManager()
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100)
        oid = om.stage(o)
        result = om.cancel_order(oid)
        assert result
        assert o.status == OrderStatus.CANCELLED

    def test_cancel_filled_order_fails(self):
        om = OrderManager()
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100, status=OrderStatus.FILLED)
        om._orders[o.order_id] = o
        result = om.cancel_order(o.order_id)
        assert not result
