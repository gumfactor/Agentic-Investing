"""Tests for FillSimulator and compute_orders."""
from __future__ import annotations

from datetime import date

import pytest

from backtesting.engine.fill_simulator import (
    FillSimulator,
    Order,
    compute_orders,
)

TODAY = date(2024, 1, 15)
PRICES = {"AAPL": 150.0, "GOOG": 100.0, "MSFT": 200.0}


# ------------------------------------------------------------------
# compute_orders
# ------------------------------------------------------------------

def test_compute_orders_sells_before_buys():
    target = {"AAPL": 0.6, "MSFT": 0.4}
    current = {"AAPL": 0.3, "GOOG": 0.5, "MSFT": 0.2}
    orders = compute_orders(target, current)
    # Sells (GOOG fully sold, partials on AAPL and MSFT increases but no sell)
    sell_indices = [i for i, o in enumerate(orders) if o.direction == "SELL"]
    buy_indices = [i for i, o in enumerate(orders) if o.direction == "BUY"]
    assert all(s < b for s in sell_indices for b in buy_indices), "Sells must come before buys"


def test_compute_orders_skips_tiny_deltas():
    target = {"AAPL": 0.5001}
    current = {"AAPL": 0.5000}
    orders = compute_orders(target, current, min_trade_weight=1e-3)
    assert len(orders) == 0


def test_compute_orders_buy_direction():
    target = {"AAPL": 0.5}
    current = {}
    orders = compute_orders(target, current)
    assert len(orders) == 1
    assert orders[0].direction == "BUY"
    assert abs(orders[0].delta_weight - 0.5) < 1e-9


def test_compute_orders_sell_direction():
    target = {}
    current = {"AAPL": 0.5}
    orders = compute_orders(target, current)
    assert len(orders) == 1
    assert orders[0].direction == "SELL"
    assert abs(orders[0].delta_weight - (-0.5)) < 1e-9


def test_compute_orders_no_orders_when_equal():
    weights = {"AAPL": 0.5, "GOOG": 0.5}
    orders = compute_orders(weights, weights.copy())
    assert len(orders) == 0


# ------------------------------------------------------------------
# FillSimulator — perfect mode
# ------------------------------------------------------------------

def test_perfect_fill_zero_costs():
    sim = FillSimulator(fill_model="perfect")
    orders = [Order("AAPL", "BUY", 0.5, 0.0, 0.5)]
    fills = sim.simulate_fills(orders, PRICES, TODAY, 100_000.0)
    assert len(fills) == 1
    f = fills[0]
    assert f.commission == 0.0
    assert f.market_impact == 0.0
    assert f.total_cost == 0.0
    assert f.fill_price == PRICES["AAPL"]


def test_perfect_fill_sell_zero_costs():
    sim = FillSimulator(fill_model="perfect")
    orders = [Order("AAPL", "SELL", 0.0, 0.5, -0.5)]
    fills = sim.simulate_fills(orders, PRICES, TODAY, 100_000.0)
    assert fills[0].commission == 0.0
    assert fills[0].total_cost == 0.0


# ------------------------------------------------------------------
# FillSimulator — transaction_cost mode
# ------------------------------------------------------------------

def test_transaction_cost_buy_fills_at_ask():
    sim = FillSimulator(bid_ask_spread_bps=20.0, fill_model="transaction_cost")
    orders = [Order("AAPL", "BUY", 0.5, 0.0, 0.5)]
    fills = sim.simulate_fills(orders, PRICES, TODAY, 100_000.0)
    f = fills[0]
    half_spread = 20.0 / 20000.0
    expected_price = PRICES["AAPL"] * (1 + half_spread)
    assert abs(f.fill_price - expected_price) < 1e-6


def test_transaction_cost_sell_fills_at_bid():
    sim = FillSimulator(bid_ask_spread_bps=20.0, fill_model="transaction_cost")
    orders = [Order("AAPL", "SELL", 0.0, 0.5, -0.5)]
    fills = sim.simulate_fills(orders, PRICES, TODAY, 100_000.0)
    f = fills[0]
    half_spread = 20.0 / 20000.0
    expected_price = PRICES["AAPL"] * (1 - half_spread)
    assert abs(f.fill_price - expected_price) < 1e-6


def test_transaction_cost_commission_applied():
    sim = FillSimulator(commission_per_share=0.01, bid_ask_spread_bps=0, fill_model="transaction_cost")
    orders = [Order("AAPL", "BUY", 0.1, 0.0, 0.1)]
    fills = sim.simulate_fills(orders, {"AAPL": 100.0}, TODAY, 100_000.0)
    f = fills[0]
    # notional = 0.1 * 100_000 = 10_000; shares = 10_000/100 = 100; commission = 100 * 0.01 = 1.0
    assert abs(f.commission - 1.0) < 1e-6


def test_transaction_cost_market_impact_positive():
    sim = FillSimulator(market_impact_coeff=0.5, bid_ask_spread_bps=0, fill_model="transaction_cost")
    orders = [Order("AAPL", "BUY", 0.5, 0.0, 0.5)]
    fills = sim.simulate_fills(orders, PRICES, TODAY, 100_000.0)
    assert fills[0].market_impact > 0


def test_missing_price_skipped():
    sim = FillSimulator(fill_model="perfect")
    orders = [Order("UNKNOWN", "BUY", 0.5, 0.0, 0.5)]
    fills = sim.simulate_fills(orders, PRICES, TODAY, 100_000.0)
    assert len(fills) == 0


def test_invalid_fill_model_raises():
    with pytest.raises(ValueError, match="Unknown fill_model"):
        FillSimulator(fill_model="magic")


def test_market_impact_with_adv():
    sim = FillSimulator(market_impact_coeff=0.5, bid_ask_spread_bps=0, fill_model="transaction_cost")
    orders = [Order("AAPL", "BUY", 0.1, 0.0, 0.1)]
    # Large ADV → low participation → low impact
    high_adv = {"AAPL": 10_000_000.0}
    fills_high_adv = sim.simulate_fills(orders, PRICES, TODAY, 100_000.0, adv_shares=high_adv)
    # Small ADV → high participation → high impact
    low_adv = {"AAPL": 100.0}
    fills_low_adv = sim.simulate_fills(orders, PRICES, TODAY, 100_000.0, adv_shares=low_adv)
    assert fills_high_adv[0].market_impact < fills_low_adv[0].market_impact
