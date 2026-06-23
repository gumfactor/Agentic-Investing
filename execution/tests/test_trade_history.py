"""Tests for TradeJournal: fill persistence, FIFO P&L, and wash-sale context."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from execution.oms.order import Order, OrderSide, OrderStatus
from execution.oms.trade_history import FillRecord, TradeJournal


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def engine():
    """SQLite in-memory engine with trade_fills schema created."""
    eng = sa.create_engine("sqlite:///:memory:", future=True)
    TradeJournal.create_schema(eng)
    return eng


@pytest.fixture()
def journal(engine):
    return TradeJournal(engine)


def _filled_order(
    ticker: str = "AAPL",
    side: OrderSide = OrderSide.BUY,
    quantity: float = 100.0,
    avg_fill_price: float = 150.0,
    limit_price: float | None = 150.0,
    strategy_id: str = "strat1",
    status: OrderStatus = OrderStatus.FILLED,
) -> Order:
    o = Order(
        ticker=ticker,
        side=side,
        quantity=quantity,
        limit_price=limit_price,
        strategy_id=strategy_id,
    )
    o.filled_quantity = quantity
    o.avg_fill_price = avg_fill_price
    # Force status directly — skipping full state-machine for test brevity
    o.status = status
    o.updated_at = datetime.now(timezone.utc)
    return o


# ── record_fill ───────────────────────────────────────────────────────────────

class TestRecordFill:
    def test_buy_fill_stored_correctly(self, journal):
        order = _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=150.0)
        rec = journal.record_fill(order)

        assert isinstance(rec, FillRecord)
        assert rec.ticker == "AAPL"
        assert rec.side == "BUY"
        assert rec.filled_quantity == pytest.approx(100.0)            # incremental
        assert rec.cumulative_filled_quantity == pytest.approx(100.0)  # same for first fill
        assert rec.avg_fill_price == pytest.approx(150.0)
        # BUYs have no realized P&L
        assert rec.realized_pnl is None
        assert rec.cost_basis_per_share is None

    def test_sell_profit_fill_computes_pnl(self, journal):
        # BUY 100 @ 100
        buy = _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=100.0)
        journal.record_fill(buy)

        # SELL 100 @ 120 → profit = 20 * 100 = 2000
        sell = _filled_order(side=OrderSide.SELL, quantity=100, avg_fill_price=120.0)
        rec = journal.record_fill(sell)

        assert rec.realized_pnl == pytest.approx(2000.0)
        assert rec.cost_basis_per_share == pytest.approx(100.0)

    def test_sell_loss_fill_computes_negative_pnl(self, journal):
        buy = _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=150.0)
        journal.record_fill(buy)

        sell = _filled_order(side=OrderSide.SELL, quantity=100, avg_fill_price=130.0)
        rec = journal.record_fill(sell)

        assert rec.realized_pnl == pytest.approx(-2000.0)
        assert rec.cost_basis_per_share == pytest.approx(150.0)

    def test_fifo_two_lots_partial_sell(self, journal):
        # Lot 1: 50 @ 100
        buy1 = _filled_order(side=OrderSide.BUY, quantity=50, avg_fill_price=100.0)
        journal.record_fill(buy1)
        # Lot 2: 50 @ 200
        buy2 = _filled_order(side=OrderSide.BUY, quantity=50, avg_fill_price=200.0)
        journal.record_fill(buy2)

        # SELL 50 @ 150 — should consume lot 1 (FIFO): cost basis = 100
        sell = _filled_order(side=OrderSide.SELL, quantity=50, avg_fill_price=150.0)
        rec = journal.record_fill(sell)

        assert rec.cost_basis_per_share == pytest.approx(100.0)
        # P&L = (150 - 100) * 50 = 2500
        assert rec.realized_pnl == pytest.approx(2500.0)

    def test_fifo_two_lots_sell_spans_lots(self, journal):
        # Lot 1: 30 @ 100, Lot 2: 70 @ 200
        buy1 = _filled_order(side=OrderSide.BUY, quantity=30, avg_fill_price=100.0)
        buy2 = _filled_order(side=OrderSide.BUY, quantity=70, avg_fill_price=200.0)
        journal.record_fill(buy1)
        journal.record_fill(buy2)

        # SELL 80 @ 180 — consumes all of lot 1 (30 @ 100) + 50 of lot 2 (@ 200)
        # weighted cost = (30*100 + 50*200) / 80 = (3000 + 10000) / 80 = 162.5
        sell = _filled_order(side=OrderSide.SELL, quantity=80, avg_fill_price=180.0)
        rec = journal.record_fill(sell)

        assert rec.cost_basis_per_share == pytest.approx(162.5)
        # P&L = (180 - 162.5) * 80 = 1400
        assert rec.realized_pnl == pytest.approx(1400.0)

    def test_fifo_sequential_sells(self, journal):
        # 100 shares @ 100
        buy = _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=100.0)
        journal.record_fill(buy)

        # SELL 40 @ 120 → P&L = (120-100)*40 = 800
        sell1 = _filled_order(side=OrderSide.SELL, quantity=40, avg_fill_price=120.0)
        rec1 = journal.record_fill(sell1)
        assert rec1.realized_pnl == pytest.approx(800.0)

        # SELL 60 @ 90 → uses remaining 60 shares at cost 100 → P&L = (90-100)*60 = -600
        sell2 = _filled_order(side=OrderSide.SELL, quantity=60, avg_fill_price=90.0)
        rec2 = journal.record_fill(sell2)
        assert rec2.realized_pnl == pytest.approx(-600.0)

    def test_sell_exceeds_open_quantity_raises(self, journal):
        buy = _filled_order(side=OrderSide.BUY, quantity=50, avg_fill_price=100.0)
        journal.record_fill(buy)

        sell = _filled_order(side=OrderSide.SELL, quantity=100, avg_fill_price=120.0)
        with pytest.raises(ValueError, match="long-only violation"):
            journal.record_fill(sell)

    def test_duplicate_fill_raises(self, journal):
        order = _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=150.0)
        journal.record_fill(order)

        with pytest.raises(ValueError, match="no new fill"):
            journal.record_fill(order)

    def test_no_fill_data_raises(self, journal):
        order = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100)
        order.status = OrderStatus.FILLED
        with pytest.raises(ValueError, match="no fill data"):
            journal.record_fill(order)

    def test_wrong_status_raises(self, journal):
        order = Order(ticker="AAPL", side=OrderSide.BUY, quantity=100)
        order.filled_quantity = 100.0
        order.avg_fill_price = 150.0
        order.status = OrderStatus.SUBMITTED  # not FILLED or PARTIALLY_FILLED
        with pytest.raises(ValueError, match="not in a filled state"):
            journal.record_fill(order)

    def test_partial_fill_recorded(self, journal):
        order = _filled_order(
            side=OrderSide.BUY,
            quantity=100,
            avg_fill_price=150.0,
            status=OrderStatus.PARTIALLY_FILLED,
        )
        order.filled_quantity = 60.0
        rec = journal.record_fill(order)
        assert rec.order_status_at_record == "PARTIALLY_FILLED"
        assert rec.filled_quantity == pytest.approx(60.0)            # incremental
        assert rec.cumulative_filled_quantity == pytest.approx(60.0)  # same as incremental (first fill)

    def test_partial_to_full_fill_no_double_count(self, journal):
        """Regression: PARTIALLY_FILLED→FILLED progression must not double-count
        lots in FIFO or produce wrong P&L.  This was the bug caught in
        adversarial review (BLOCKER #1)."""
        # BUY 100 @ 100
        buy = _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=100.0)
        journal.record_fill(buy)

        # Same SELL order: partial fill at 60 shares, then full fill at 100 shares
        sell_order = Order(
            ticker="AAPL", side=OrderSide.SELL, quantity=100, limit_price=120.0,
            strategy_id="strat1",
        )

        # PARTIALLY_FILLED: 60 shares @ 120
        sell_order.filled_quantity = 60.0
        sell_order.avg_fill_price = 120.0
        sell_order.status = OrderStatus.PARTIALLY_FILLED
        sell_order.updated_at = datetime.now(timezone.utc)
        rec1 = journal.record_fill(sell_order)

        assert rec1.filled_quantity == pytest.approx(60.0)           # incremental
        assert rec1.cumulative_filled_quantity == pytest.approx(60.0)
        assert rec1.realized_pnl == pytest.approx(1200.0)            # (120-100)*60

        # FILLED: 100 shares cumulative @ blended avg 118.
        # Incremental 40 shares actually executed at 115:
        #   (118*100 - 120*60) / 40 = (11800 - 7200) / 40 = 115.0
        sell_order.filled_quantity = 100.0
        sell_order.avg_fill_price = 118.0
        sell_order.status = OrderStatus.FILLED
        sell_order.updated_at = datetime.now(timezone.utc)
        rec2 = journal.record_fill(sell_order)

        assert rec2.filled_quantity == pytest.approx(40.0)            # incremental (100-60)
        assert rec2.cumulative_filled_quantity == pytest.approx(100.0)
        # Back-calculated incremental price = 115; P&L = (115-100)*40 = 600
        assert rec2.avg_fill_price == pytest.approx(115.0)
        assert rec2.realized_pnl == pytest.approx(600.0)

        # Total P&L across both events = 1200 + 600 = 1800
        summary = journal.realized_pnl_summary()
        assert summary["AAPL"] == pytest.approx(1800.0)

        # Open position should be zero (all 100 shares sold)
        open_pos = journal.open_position_cost_basis()
        assert "AAPL" not in open_pos

        # No further sell should raise long-only violation (position is flat)
        follow_sell = _filled_order(
            side=OrderSide.SELL, quantity=10, avg_fill_price=115.0
        )
        with pytest.raises(ValueError, match="long-only violation"):
            journal.record_fill(follow_sell)


# ── wash_sale_context ─────────────────────────────────────────────────────────

class TestWashSaleContext:
    def test_empty_tickers_returns_empty(self, journal):
        result = journal.wash_sale_context([])
        assert result == {}

    def test_no_fills_returns_empty(self, journal):
        result = journal.wash_sale_context(["AAPL", "MSFT"])
        assert result == {}

    def test_recent_loss_sell_included(self, journal):
        buy = _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=150.0)
        journal.record_fill(buy)

        sell = _filled_order(side=OrderSide.SELL, quantity=100, avg_fill_price=120.0)
        journal.record_fill(sell)

        as_of = datetime.now(timezone.utc).date() + timedelta(days=10)
        result = journal.wash_sale_context(["AAPL"], as_of=as_of)
        assert "AAPL" in result
        assert isinstance(result["AAPL"], date)

    def test_old_loss_sell_excluded(self, journal):
        buy = _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=150.0)
        journal.record_fill(buy)
        sell = _filled_order(side=OrderSide.SELL, quantity=100, avg_fill_price=120.0)
        journal.record_fill(sell)

        # as_of is 31 days after the fill — outside the 30-day window
        as_of = datetime.now(timezone.utc).date() + timedelta(days=31)
        result = journal.wash_sale_context(["AAPL"], as_of=as_of)
        assert "AAPL" not in result

    def test_profit_sell_excluded(self, journal):
        buy = _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=100.0)
        journal.record_fill(buy)
        sell = _filled_order(side=OrderSide.SELL, quantity=100, avg_fill_price=120.0)
        journal.record_fill(sell)  # profit sell

        as_of = datetime.now(timezone.utc).date() + timedelta(days=10)
        result = journal.wash_sale_context(["AAPL"], as_of=as_of)
        assert "AAPL" not in result

    def test_unrelated_ticker_excluded(self, journal):
        buy = _filled_order(ticker="MSFT", side=OrderSide.BUY, quantity=100, avg_fill_price=150.0)
        journal.record_fill(buy)
        sell = _filled_order(ticker="MSFT", side=OrderSide.SELL, quantity=100, avg_fill_price=120.0)
        journal.record_fill(sell)

        as_of = datetime.now(timezone.utc).date() + timedelta(days=5)
        # Only querying AAPL — MSFT loss sell should not appear
        result = journal.wash_sale_context(["AAPL"], as_of=as_of)
        assert "AAPL" not in result
        assert "MSFT" not in result


# ── fill_history ──────────────────────────────────────────────────────────────

class TestFillHistory:
    def test_all_fills_returned(self, journal):
        for _ in range(3):
            journal.record_fill(
                _filled_order(side=OrderSide.BUY, quantity=10, avg_fill_price=100.0)
            )
        assert len(journal.fill_history()) == 3

    def test_filter_by_ticker(self, journal):
        journal.record_fill(
            _filled_order(ticker="AAPL", side=OrderSide.BUY, quantity=10, avg_fill_price=100.0)
        )
        journal.record_fill(
            _filled_order(ticker="MSFT", side=OrderSide.BUY, quantity=10, avg_fill_price=200.0)
        )
        result = journal.fill_history(ticker="AAPL")
        assert len(result) == 1
        assert result[0].ticker == "AAPL"

    def test_filter_by_strategy(self, journal):
        journal.record_fill(
            _filled_order(strategy_id="s1", side=OrderSide.BUY, quantity=10, avg_fill_price=100.0)
        )
        journal.record_fill(
            _filled_order(strategy_id="s2", side=OrderSide.BUY, quantity=10, avg_fill_price=100.0)
        )
        result = journal.fill_history(strategy_id="s1")
        assert len(result) == 1
        assert result[0].strategy_id == "s1"

    def test_filter_by_since(self, journal):
        journal.record_fill(
            _filled_order(side=OrderSide.BUY, quantity=10, avg_fill_price=100.0)
        )
        tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
        result = journal.fill_history(since=tomorrow)
        assert len(result) == 0


# ── realized_pnl_summary ─────────────────────────────────────────────────────

class TestRealizedPnlSummary:
    def test_no_fills_returns_empty(self, journal):
        assert journal.realized_pnl_summary() == {}

    def test_buy_only_returns_empty(self, journal):
        journal.record_fill(
            _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=100.0)
        )
        assert journal.realized_pnl_summary() == {}

    def test_single_trade_pnl(self, journal):
        journal.record_fill(
            _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=100.0)
        )
        journal.record_fill(
            _filled_order(side=OrderSide.SELL, quantity=100, avg_fill_price=110.0)
        )
        summary = journal.realized_pnl_summary()
        assert "AAPL" in summary
        assert summary["AAPL"] == pytest.approx(1000.0)

    def test_multiple_trades_sum(self, journal):
        # Trade 1: profit 1000
        journal.record_fill(
            _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=100.0)
        )
        journal.record_fill(
            _filled_order(side=OrderSide.SELL, quantity=100, avg_fill_price=110.0)
        )
        # Trade 2: rebuy and sell at loss -500
        journal.record_fill(
            _filled_order(side=OrderSide.BUY, quantity=50, avg_fill_price=120.0)
        )
        journal.record_fill(
            _filled_order(side=OrderSide.SELL, quantity=50, avg_fill_price=110.0)
        )
        summary = journal.realized_pnl_summary()
        assert summary["AAPL"] == pytest.approx(1000.0 + (-500.0))


# ── open_position_cost_basis ──────────────────────────────────────────────────

class TestOpenPositionCostBasis:
    def test_no_fills_returns_empty(self, journal):
        assert journal.open_position_cost_basis() == {}

    def test_buy_only_open_position(self, journal):
        journal.record_fill(
            _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=150.0)
        )
        result = journal.open_position_cost_basis()
        assert "AAPL" in result
        qty, cost = result["AAPL"]
        assert qty == pytest.approx(100.0)
        assert cost == pytest.approx(150.0)

    def test_partial_sell_reduces_open_qty(self, journal):
        journal.record_fill(
            _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=100.0)
        )
        journal.record_fill(
            _filled_order(side=OrderSide.SELL, quantity=40, avg_fill_price=120.0)
        )
        result = journal.open_position_cost_basis()
        qty, cost = result["AAPL"]
        assert qty == pytest.approx(60.0)
        assert cost == pytest.approx(100.0)  # FIFO cost basis of remaining lot

    def test_fully_sold_position_excluded(self, journal):
        journal.record_fill(
            _filled_order(side=OrderSide.BUY, quantity=100, avg_fill_price=100.0)
        )
        journal.record_fill(
            _filled_order(side=OrderSide.SELL, quantity=100, avg_fill_price=120.0)
        )
        result = journal.open_position_cost_basis()
        assert "AAPL" not in result

    def test_fifo_cost_basis_after_partial_sell(self, journal):
        # Two lots: 50 @ 100 and 50 @ 200
        buy1 = _filled_order(side=OrderSide.BUY, quantity=50, avg_fill_price=100.0)
        buy2 = _filled_order(side=OrderSide.BUY, quantity=50, avg_fill_price=200.0)
        journal.record_fill(buy1)
        journal.record_fill(buy2)

        # SELL 50 — consumes lot 1 (FIFO); remaining = 50 @ 200
        sell = _filled_order(side=OrderSide.SELL, quantity=50, avg_fill_price=150.0)
        journal.record_fill(sell)

        result = journal.open_position_cost_basis()
        qty, cost = result["AAPL"]
        assert qty == pytest.approx(50.0)
        assert cost == pytest.approx(200.0)
