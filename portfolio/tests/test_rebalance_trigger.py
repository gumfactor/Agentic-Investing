"""Tests for RebalanceTrigger."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from portfolio.rebalancing.trigger import RebalanceFrequency, RebalanceTrigger


class TestRebalanceTrigger:
    def test_first_rebalance_always_fires(self):
        trigger = RebalanceTrigger(frequency=RebalanceFrequency.MONTHLY)
        should, reason = trigger.should_rebalance(date(2024, 1, 15), trading_days_since_last=0)
        assert should

    def test_monthly_fires_on_new_month(self):
        trigger = RebalanceTrigger(frequency=RebalanceFrequency.MONTHLY)
        trigger.record_rebalance(date(2024, 1, 15))
        for _ in range(21):
            trigger.advance_day()
        should, reason = trigger.should_rebalance(date(2024, 2, 15), trading_days_since_last=21)
        assert should
        assert "calendar:monthly" in reason

    def test_monthly_does_not_fire_same_month(self):
        trigger = RebalanceTrigger(frequency=RebalanceFrequency.MONTHLY, min_holding_days=1)
        trigger.record_rebalance(date(2024, 1, 10))
        should, reason = trigger.should_rebalance(date(2024, 1, 20), trading_days_since_last=7)
        assert not should

    def test_drift_trigger(self):
        trigger = RebalanceTrigger(
            frequency=RebalanceFrequency.MONTHLY,
            drift_threshold=0.10,
            min_holding_days=1,
        )
        trigger.record_rebalance(date(2024, 1, 10))
        current = pd.Series({"AAPL": 0.60, "MSFT": 0.40})
        target = pd.Series({"AAPL": 0.40, "MSFT": 0.60})
        should, reason = trigger.should_rebalance(
            date(2024, 1, 20),
            current_weights=current,
            target_weights=target,
            trading_days_since_last=5,
        )
        assert should
        assert "drift" in reason

    def test_min_holding_days_blocks_rebalance(self):
        trigger = RebalanceTrigger(
            frequency=RebalanceFrequency.DAILY,
            min_holding_days=5,
        )
        trigger.record_rebalance(date(2024, 1, 10))
        should, reason = trigger.should_rebalance(date(2024, 1, 11), trading_days_since_last=1)
        assert not should
        assert "min_holding_days" in reason

    def test_weekly_fires_monday(self):
        trigger = RebalanceTrigger(frequency=RebalanceFrequency.WEEKLY, min_holding_days=1)
        trigger.record_rebalance(date(2024, 1, 8))  # Monday
        # Next Monday
        should, reason = trigger.should_rebalance(date(2024, 1, 15), trading_days_since_last=5)
        assert should

    def test_record_rebalance_resets_counter(self):
        trigger = RebalanceTrigger()
        trigger.record_rebalance(date(2024, 1, 10))
        assert trigger._trading_days_since == 0

    def test_advance_day_increments(self):
        trigger = RebalanceTrigger()
        for _ in range(5):
            trigger.advance_day()
        assert trigger._trading_days_since == 5
