"""Unit tests for signals/factors/_fundamentals_utils.py."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signals.factors._fundamentals_utils import (
    align_fundamentals,
    compute_ev_wide,
    fund_to_wide,
    validate_fundamentals,
)
from signals.tests.factors.conftest import make_fixed_prices, make_fundamentals


# ─── validate_fundamentals ────────────────────────────────────────────────────

def test_validate_passes_with_all_columns():
    fund = make_fundamentals(["A", "B"], eps_ttm=5.0)
    validate_fundamentals(fund, {"eps_ttm"})  # no exception


def test_validate_raises_on_missing_date_column():
    fund = pd.DataFrame({"ticker": ["A"], "eps_ttm": [5.0]})
    with pytest.raises(ValueError, match="missing base columns"):
        validate_fundamentals(fund, {"eps_ttm"})


def test_validate_raises_on_missing_ticker_column():
    fund = pd.DataFrame({"date": [pd.Timestamp("2020-01-01")], "eps_ttm": [5.0]})
    with pytest.raises(ValueError, match="missing base columns"):
        validate_fundamentals(fund, {"eps_ttm"})


def test_validate_raises_on_missing_metric_column():
    fund = pd.DataFrame({"date": [pd.Timestamp("2020-01-01")], "ticker": ["A"]})
    with pytest.raises(ValueError, match="missing required metric columns"):
        validate_fundamentals(fund, {"eps_ttm"})


def test_validate_raises_on_empty_dataframe():
    fund = pd.DataFrame({"date": [], "ticker": [], "eps_ttm": []})
    with pytest.raises(ValueError, match="empty"):
        validate_fundamentals(fund, {"eps_ttm"})


def test_validate_accepts_multiple_required_metrics():
    fund = make_fundamentals(["A"], eps_ttm=1.0, revenue_ttm=100.0)
    validate_fundamentals(fund, {"eps_ttm", "revenue_ttm"})  # no exception


# ─── fund_to_wide ─────────────────────────────────────────────────────────────

def test_fund_to_wide_shape():
    fund = make_fundamentals(["A", "B", "C"], n_quarters=8, eps_ttm=5.0)
    wide = fund_to_wide(fund, "eps_ttm")
    assert wide.shape == (8, 3)
    assert set(wide.columns) == {"A", "B", "C"}


def test_fund_to_wide_values_are_float():
    fund = make_fundamentals(["A"], eps_ttm=7)
    wide = fund_to_wide(fund, "eps_ttm")
    assert wide.dtypes["A"] == float


def test_fund_to_wide_index_is_sorted():
    fund = make_fundamentals(["A"], n_quarters=4, eps_ttm=1.0)
    wide = fund_to_wide(fund, "eps_ttm")
    assert wide.index.is_monotonic_increasing


# ─── align_fundamentals ───────────────────────────────────────────────────────

def test_align_fundamentals_forward_fills_to_price_index():
    """Quarterly value propagates forward to daily price dates."""
    fund_wide = pd.DataFrame(
        {"A": [10.0], "B": [20.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2020-01-01")]),
    )
    price_index = pd.bdate_range("2020-01-01", periods=63)
    aligned = align_fundamentals(fund_wide, price_index)

    assert aligned.shape == (63, 2)
    assert (aligned["A"] == 10.0).all()
    assert (aligned["B"] == 20.0).all()


def test_align_fundamentals_no_look_ahead():
    """Dates before the first fundamental date produce NaN (no future look-ahead)."""
    fund_wide = pd.DataFrame(
        {"A": [5.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2020-06-01")]),
    )
    # Price index starts before the fundamental date
    price_index = pd.bdate_range("2020-01-01", periods=100)
    aligned = align_fundamentals(fund_wide, price_index)

    # Rows before 2020-06-01 should be NaN
    before = aligned[aligned.index < pd.Timestamp("2020-06-01")]
    assert before["A"].isna().all()

    # Rows on or after 2020-06-01 should have the value
    on_or_after = aligned[aligned.index >= pd.Timestamp("2020-06-01")]
    assert (on_or_after["A"] == 5.0).all()


def test_align_fundamentals_new_quarter_replaces_old():
    """When a new quarterly value arrives, it supersedes the previous one."""
    fund_wide = pd.DataFrame(
        {"A": [10.0, 20.0]},
        index=pd.DatetimeIndex([
            pd.Timestamp("2020-01-01"),
            pd.Timestamp("2020-04-01"),
        ]),
    )
    price_index = pd.bdate_range("2020-01-01", periods=130)
    aligned = align_fundamentals(fund_wide, price_index)

    before_q2 = aligned[aligned.index < pd.Timestamp("2020-04-01")]["A"]
    on_q2_and_after = aligned[aligned.index >= pd.Timestamp("2020-04-01")]["A"]
    assert (before_q2 == 10.0).all()
    assert (on_q2_and_after == 20.0).all()


def test_align_fundamentals_returns_price_index_shape():
    fund_wide = pd.DataFrame(
        {"A": [1.0], "B": [2.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2019-01-01")]),
    )
    price_index = pd.bdate_range("2020-01-01", periods=50)
    aligned = align_fundamentals(fund_wide, price_index)
    assert len(aligned) == 50
    assert aligned.index.equals(price_index)


# ─── compute_ev_wide ──────────────────────────────────────────────────────────

def test_compute_ev_wide_formula():
    """EV = Price × Shares + Debt − Cash for a single-date, single-ticker case."""
    price = 100.0
    shares = 10.0
    debt = 500.0
    cash = 100.0
    expected_ev = price * shares + debt - cash  # 1400.0

    prices = pd.DataFrame([{"date": pd.Timestamp("2020-01-02"), "ticker": "X", "close": price}])
    fund = pd.DataFrame([{"date": pd.Timestamp("2019-01-01"), "ticker": "X",
                          "shares_outstanding": shares, "total_debt": debt, "cash": cash}])

    from signals.factors._price_utils import to_wide
    price_wide = to_wide(prices)
    ev_wide = compute_ev_wide(price_wide, fund)

    assert ev_wide.loc[pd.Timestamp("2020-01-02"), "X"] == pytest.approx(expected_ev)


def test_compute_ev_wide_negative_ev_is_possible():
    """Net-cash companies (cash > market cap + debt) produce negative EV."""
    prices = pd.DataFrame([{"date": pd.Timestamp("2020-01-02"), "ticker": "X", "close": 10.0}])
    fund = pd.DataFrame([{"date": pd.Timestamp("2019-01-01"), "ticker": "X",
                          "shares_outstanding": 1.0, "total_debt": 0.0, "cash": 1000.0}])

    from signals.factors._price_utils import to_wide
    price_wide = to_wide(prices)
    ev_wide = compute_ev_wide(price_wide, fund)
    # EV = 10*1 + 0 - 1000 = -990
    assert ev_wide.loc[pd.Timestamp("2020-01-02"), "X"] < 0
