"""Tests for the OMS state machine, compliance, and order manager."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from execution.oms.compliance import ComplianceEngine
from execution.oms.order import Order, OrderSide, OrderStatus
from execution.oms.order_manager import OrderManager
from execution.oms.trade_history import TradeJournal


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

    def test_partially_filled_not_terminal(self):
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100, status=OrderStatus.SUBMITTED)
        o.transition(OrderStatus.PARTIALLY_FILLED)
        assert not o.is_terminal
        assert o.is_partial

    def test_partially_filled_to_filled_transition(self):
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100, status=OrderStatus.SUBMITTED)
        o.transition(OrderStatus.PARTIALLY_FILLED)
        o.transition(OrderStatus.FILLED)
        assert o.is_terminal

    def test_fill_fraction(self):
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100)
        o.filled_quantity = 40.0
        assert o.fill_fraction == pytest.approx(0.40)

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
            "circuit_breaker_open": False,
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
            "circuit_breaker_open": False,
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
            "circuit_breaker_open": False,
            "recent_loss_buys": {"AAPL": date(2024, 1, 1)},
            "as_of_date": date(2024, 1, 15),
        }
        o = self._order(side=OrderSide.BUY)
        passed, _ = engine.check(o, ctx)
        assert passed

    def test_min_notional_rejected(self):
        engine = ComplianceEngine()
        ctx = {"circuit_breaker_open": False, "min_order_notional": 1000.0}
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=1, limit_price=50.0)
        passed, reason = engine.check(o, ctx)
        assert not passed
        assert "below_min_notional" in reason

    def test_clean_order_passes(self):
        engine = ComplianceEngine()
        ctx = {"circuit_breaker_open": False, "min_order_notional": 0.0}
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
        ctx = {"circuit_breaker_open": False, "min_order_notional": 0.0}
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
        om.run_compliance({"circuit_breaker_open": False, "min_order_notional": 0.0})
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

    def test_toctou_circuit_breaker_opens_between_compliance_and_submit(self):
        """CB opens after compliance passes but before submit_pending() — must be caught."""
        from unittest.mock import MagicMock
        from risk.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        mock_broker = MagicMock()
        om = OrderManager(broker=mock_broker, circuit_breaker=cb)

        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100, limit_price=150.0)
        om.stage(o)
        om.run_compliance({"min_order_notional": 0.0})
        assert o.status == OrderStatus.PENDING

        # Trip CB externally between compliance and submission
        snap = MagicMock()
        snap.circuit_breaker_tripped = True
        snap.breaches = [{"severity": "hard", "metric": "drawdown", "value": -0.15, "threshold": -0.10}]
        import datetime
        snap.as_of = datetime.date.today()
        cb.evaluate(snap)
        assert cb.is_open

        with pytest.raises(RuntimeError, match="Circuit breaker"):
            om.submit_pending()

    def test_end_to_end_breach_trips_and_blocks_orders(self):
        """Full safety chain: RiskMonitor hard breach → CB trips → OrderManager blocks."""
        import datetime
        import numpy as np
        import pandas as pd
        from unittest.mock import MagicMock
        from risk.realtime.monitor import RiskMonitor
        from risk.circuit_breaker import CircuitBreaker

        # Monitor with very tight thresholds
        monitor = RiskMonitor(
            hard_drawdown=-0.05, warn_drawdown=-0.02,
            hard_var=0.20, warn_var=0.15,
            hard_beta=5.0, warn_beta=4.0,
            hard_concentration=0.50, warn_concentration=0.40,
        )
        cb = CircuitBreaker()

        # First snapshot sets peak NAV
        rng = np.random.default_rng(42)
        portfolio_returns = pd.Series(rng.normal(0.001, 0.008, 60))
        asset_returns = pd.DataFrame({"AAPL": portfolio_returns})
        benchmark_returns = pd.Series(rng.normal(0.001, 0.007, 60))
        weights = pd.Series({"AAPL": 0.10})

        monitor.snapshot(
            datetime.date(2024, 1, 1), 1_000_000.0, weights,
            portfolio_returns, asset_returns, benchmark_returns,
        )

        # Second snapshot with hard drawdown (-10% from peak)
        snap = monitor.snapshot(
            datetime.date(2024, 1, 2), 940_000.0, weights,
            portfolio_returns, asset_returns, benchmark_returns,
        )
        assert snap.circuit_breaker_tripped

        # Trip the circuit breaker
        cb.evaluate(snap)
        assert cb.is_open

        # OrderManager should block submission
        mock_broker = MagicMock()
        om = OrderManager(broker=mock_broker, circuit_breaker=cb)
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100, limit_price=150.0)
        om.stage(o)
        approved, _ = om.run_compliance({"min_order_notional": 0.0})
        assert len(approved) == 0  # blocked by CB-injected context

        with pytest.raises(RuntimeError, match="Circuit breaker"):
            om.submit_pending()


# ── TradeJournal integration ──────────────────────────────────────────────────

class TestOrderManagerJournalIntegration:
    def _make_journal(self):
        engine = sa.create_engine("sqlite:///:memory:", future=True)
        TradeJournal.create_schema(engine)
        return TradeJournal(engine)

    def _fake_broker(self, filled_qty: float, avg_price: float):
        broker = MagicMock()
        broker.submit_order.return_value = "broker-001"
        broker.get_fill.return_value = {
            "filled_quantity": filled_qty,
            "avg_price": avg_price,
            "status": "FILLED",
        }
        return broker

    def test_reconcile_records_fill_to_journal(self):
        journal = self._make_journal()
        broker = self._fake_broker(filled_qty=100.0, avg_price=150.0)

        om = OrderManager(broker=broker, trade_journal=journal)
        o = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100, limit_price=150.0)
        om.stage(o)
        om.run_compliance({"circuit_breaker_open": False, "min_order_notional": 0.0})
        om.submit_pending()
        om.reconcile_fills()

        fills = journal.fill_history(ticker="AAPL")
        assert len(fills) == 1
        assert fills[0].ticker == "AAPL"
        assert fills[0].side == "BUY"
        assert fills[0].filled_quantity == pytest.approx(100.0)

    def test_compliance_wash_sale_populated_from_journal(self):
        """Wash-sale context from a real journal unblocks _check_wash_sale."""
        journal = self._make_journal()

        # Record a loss-realizing SELL fill directly into the journal
        buy_order = Order(
            ticker="AAPL", side=OrderSide.BUY, quantity=100, limit_price=150.0,
            strategy_id="s1",
        )
        buy_order.filled_quantity = 100.0
        buy_order.avg_fill_price = 150.0
        buy_order.status = OrderStatus.FILLED
        buy_order.updated_at = datetime.now(timezone.utc)
        journal.record_fill(buy_order)

        sell_order = Order(
            ticker="AAPL", side=OrderSide.SELL, quantity=100, limit_price=100.0,
            strategy_id="s1",
        )
        sell_order.filled_quantity = 100.0
        sell_order.avg_fill_price = 100.0  # below cost → loss
        sell_order.status = OrderStatus.FILLED
        sell_order.updated_at = datetime.now(timezone.utc)
        journal.record_fill(sell_order)

        # Now stage a new SELL for the same ticker and run compliance
        om = OrderManager(trade_journal=journal)
        new_sell = Order(
            ticker="AAPL", side=OrderSide.SELL, quantity=50, limit_price=95.0,
            strategy_id="s1",
        )
        om.stage(new_sell)

        # Pass as_of_date so the wash-sale check has a reference date
        ctx = {
            "circuit_breaker_open": False,
            "min_order_notional": 0.0,
            "as_of_date": date.today(),
        }
        approved, rejected = om.run_compliance(ctx)

        # The loss SELL is within 30 days → wash-sale check should block the new SELL
        assert len(rejected) == 1
        assert "wash-sale" in rejected[0].rejection_reason
