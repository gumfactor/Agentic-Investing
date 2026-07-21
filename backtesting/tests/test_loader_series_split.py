"""Tests for BUG-070: raw execution series vs. cutoff-aware analytic series.

Covers:
  - A corporate action does not, and cannot, leak into the raw execution
    series DataHandler.get_close serves for fills/NAV (the series is never
    adjusted at all -- the core fix; the old bug was applying a single
    full-history adjustment to the one series shared by both concerns).
  - The cutoff-aware analytic series (`build_realized_total_return_as_of`
    via `backtesting.loader._build_analytic_prices`) excludes an action not
    yet known/occurred by the run-boundary cutoff (no lookahead).
  - Splits are applied to the portfolio as a share-count change; dividends
    as a cash credit -- never as a price adjustment to traded notional.
  - Fail-closed: a structurally-deficient (non-empty, missing known_at)
    corporate_actions frame aborts analytic-series construction; only a
    genuinely empty/opted-in frame proceeds without adjustment.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from backtesting.engine.data_handler import DataHandler
from backtesting.engine.event_loop import BacktestEngine, _PortfolioState
from backtesting.engine.fill_simulator import Fill
from backtesting.loader import _build_analytic_prices


def _daily_prices(tickers, n_days=30, start=date(2023, 1, 2), close_fn=None):
    rows = []
    d = start
    count = 0
    while count < n_days:
        if d.weekday() < 5:
            for t in tickers:
                close = close_fn(t, d) if close_fn else 150.0
                rows.append({"ticker": t, "date": d, "close": close, "source": "test"})
            count += 1
        d += timedelta(days=1)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------
# 1. Raw execution series is never adjusted -- no lookahead possible.
# ------------------------------------------------------------------

def test_raw_execution_series_unaffected_by_future_corporate_action():
    """A split dated well within the loaded window must not change the raw
    close DataHandler.get_close serves on any date -- proving the class of
    lookahead BUG-070 describes (a future action leaking into a historical
    fill/NAV input) cannot occur: the execution series is never adjusted at
    all, by construction."""
    split_date = date(2023, 1, 16)
    tickers = ["AAPL"]

    def close_fn(t, d):
        return 300.0 if d < split_date else 150.0  # realistic: raw price halves on ex-date

    raw_prices = _daily_prices(tickers, n_days=25, close_fn=close_fn)
    raw_before = raw_prices.copy()

    alpha_scores = pd.DataFrame(
        {"ticker": ["AAPL"], "score_date": [date(2023, 1, 3)], "alpha_score": [1.0]}
    )
    benchmark = pd.DataFrame({"date": [date(2023, 1, 3)], "close": [400.0]})
    corp_actions = pd.DataFrame([{
        "ticker": "AAPL",
        "ex_date": split_date,
        "action_type": "split",
        "value": 2.0,
        "known_at": datetime(2023, 1, 1, tzinfo=timezone.utc),
    }])

    handler = DataHandler(
        raw_prices, alpha_scores, benchmark, corporate_actions=corp_actions
    )

    pre_split_date = split_date - timedelta(days=3)
    close_before_split = handler.get_close(pre_split_date)
    expected_raw = raw_before.loc[
        raw_before["date"] == pre_split_date, "close"
    ].iloc[0]

    # The pre-split raw close is exactly what was loaded -- unmodified by
    # the split action known at/after that date. Byte-for-byte stable.
    assert close_before_split["AAPL"] == pytest.approx(float(expected_raw))
    assert close_before_split["AAPL"] == pytest.approx(300.0)


# ------------------------------------------------------------------
# 2. Analytic series respects the cutoff -- proves no lookahead there either.
# ------------------------------------------------------------------

def test_analytic_series_excludes_action_after_cutoff():
    """A split whose ex_date is AFTER the run-boundary cutoff must not be
    applied to the analytic series -- it stays unadjusted (adj_factor=1)
    for every date, exactly like an "as of" run that predates the split."""
    split_date = date(2023, 2, 15)  # after the backtest window/cutoff below
    tickers = ["AAPL"]
    raw_prices = _daily_prices(tickers, n_days=20, start=date(2023, 1, 2))

    corp_actions = pd.DataFrame([{
        "ticker": "AAPL",
        "ex_date": split_date,
        "action_type": "split",
        "value": 2.0,
        "known_at": datetime(2023, 1, 1, tzinfo=timezone.utc),
    }])

    config = {
        "backtest": {
            "start_date": "2023-01-02",
            "end_date": str(raw_prices["date"].max()),  # well before split_date
        }
    }
    analytic = _build_analytic_prices(raw_prices, corp_actions, config)

    # Every row's adjusted close must equal the raw close -- the split had
    # neither occurred nor been knowable by the run-boundary cutoff.
    merged = analytic.merge(
        raw_prices[["ticker", "date", "close"]], on=["ticker", "date"], suffixes=("_adj", "_raw")
    )
    assert (merged["close_adj"] - merged["close_raw"]).abs().max() < 1e-6


def test_analytic_series_includes_action_known_by_cutoff():
    """The complementary case: a split that HAS occurred and was known by
    the cutoff is applied to pre-split analytic dates (this is a total
    -return valuation series, not a score input -- full-history-style
    adjustment within the knowable window is correct here, per the
    corporate_actions module docstring)."""
    split_date = date(2023, 1, 10)
    tickers = ["AAPL"]

    def close_fn(t, d):
        return 300.0 if d < split_date else 150.0

    raw_prices = _daily_prices(tickers, n_days=20, close_fn=close_fn)
    corp_actions = pd.DataFrame([{
        "ticker": "AAPL",
        "ex_date": split_date,
        "action_type": "split",
        "value": 2.0,
        "known_at": datetime(2023, 1, 1, tzinfo=timezone.utc),
    }])
    config = {
        "backtest": {
            "start_date": "2023-01-02",
            "end_date": str(raw_prices["date"].max()),
        }
    }
    analytic = _build_analytic_prices(raw_prices, corp_actions, config)
    pre_split = analytic[analytic["date"] < split_date]
    assert all(abs(pre_split["close"] - 150.0) < 1e-3)


# ------------------------------------------------------------------
# 3. Portfolio-side split/dividend accounting.
# ------------------------------------------------------------------

def test_split_applied_as_share_count_change_not_price_adjustment():
    """A 2-for-1 split must double held shares and leave cash/notional
    untouched -- not adjust any traded price."""
    portfolio = _PortfolioState(cash=1000.0, positions={"AAPL": 10.0})
    actions = pd.DataFrame([
        {"ticker": "AAPL", "action_type": "split", "value": 2.0},
    ])
    portfolio.apply_corporate_actions(actions)

    assert portfolio.positions["AAPL"] == pytest.approx(20.0)
    assert portfolio.cash == pytest.approx(1000.0)  # unaffected


def test_dividend_applied_as_cash_credit_not_price_adjustment():
    """A per-share dividend must credit cash by shares_held * value and
    leave the share count unchanged."""
    portfolio = _PortfolioState(cash=500.0, positions={"AAPL": 10.0})
    actions = pd.DataFrame([
        {"ticker": "AAPL", "action_type": "dividend", "value": 1.5},
    ])
    portfolio.apply_corporate_actions(actions)

    assert portfolio.positions["AAPL"] == pytest.approx(10.0)  # unchanged
    assert portfolio.cash == pytest.approx(500.0 + 10.0 * 1.5)


def test_same_date_split_and_dividend_dividend_uses_post_split_shares():
    """A same-date split + dividend: shares double first, then the
    dividend credit uses the post-split share count (POST_SPLIT
    convention, matching data.normalization.corporate_actions)."""
    portfolio = _PortfolioState(cash=0.0, positions={"AAPL": 10.0})
    actions = pd.DataFrame([
        {"ticker": "AAPL", "action_type": "split", "value": 2.0},
        {"ticker": "AAPL", "action_type": "dividend", "value": 1.0},
    ])
    portfolio.apply_corporate_actions(actions)

    assert portfolio.positions["AAPL"] == pytest.approx(20.0)
    assert portfolio.cash == pytest.approx(20.0 * 1.0)  # post-split shares


def test_corporate_action_on_unheld_ticker_is_noop():
    """A split/dividend for a ticker not currently held must not create a
    phantom position or cash movement."""
    portfolio = _PortfolioState(cash=100.0, positions={})
    actions = pd.DataFrame([
        {"ticker": "MSFT", "action_type": "split", "value": 3.0},
        {"ticker": "MSFT", "action_type": "dividend", "value": 2.0},
    ])
    portfolio.apply_corporate_actions(actions)

    assert "MSFT" not in portfolio.positions
    assert portfolio.cash == pytest.approx(100.0)


# ------------------------------------------------------------------
# 4. Fail-closed: structurally deficient corporate_actions data aborts.
# ------------------------------------------------------------------

def test_missing_known_at_column_aborts_analytic_series_construction():
    """A non-empty corporate_actions frame missing the required `known_at`
    column must raise, never silently degrade to adj_factor=1.0."""
    raw_prices = _daily_prices(["AAPL"], n_days=10)
    corp_actions = pd.DataFrame([
        {"ticker": "AAPL", "ex_date": date(2023, 1, 5), "action_type": "split", "value": 2.0},
    ])  # no known_at column
    config = {
        "backtest": {
            "start_date": "2023-01-02",
            "end_date": str(raw_prices["date"].max()),
        }
    }
    with pytest.raises(ValueError, match="known_at"):
        _build_analytic_prices(raw_prices, corp_actions, config)


def test_empty_opted_in_corporate_actions_proceeds_unadjusted():
    """The explicit opt-in empty frame (allow_missing_corporate_actions=True
    path) is a genuine "no actions" case, not a structural deficiency -- it
    must proceed with adj_factor=1.0 everywhere, matching the existing
    03A-2/BUG-039 opt-in contract."""
    raw_prices = _daily_prices(["AAPL"], n_days=10)
    corp_actions = pd.DataFrame(columns=["ticker", "ex_date", "action_type", "value"])
    config = {
        "backtest": {
            "start_date": "2023-01-02",
            "end_date": str(raw_prices["date"].max()),
        }
    }
    analytic = _build_analytic_prices(raw_prices, corp_actions, config)
    merged = analytic.merge(
        raw_prices[["ticker", "date", "close"]], on=["ticker", "date"], suffixes=("_adj", "_raw")
    )
    assert (merged["close_adj"] - merged["close_raw"]).abs().max() < 1e-6


# ------------------------------------------------------------------
# 5. End-to-end engine test: buy-and-hold NAV continuity across a split.
# ------------------------------------------------------------------

def test_engine_nav_continuous_across_split_via_raw_prices():
    """A buy-and-hold position must show continuous NAV across a split date
    when fills/NAV use the raw price series and the portfolio applies the
    split as an explicit share-count change -- proving the raw-price path
    plus explicit accounting reproduces the correct economics without ever
    adjusting the traded price series."""
    from backtesting.engine.fill_simulator import FillSimulator

    split_date = date(2023, 1, 16)
    tickers = ["AAPL"]

    def close_fn(t, d):
        return 300.0 if d < split_date else 150.0

    raw_prices = _daily_prices(tickers, n_days=30, close_fn=close_fn)
    alpha_scores = pd.DataFrame(
        {"ticker": tickers * 3,
         "score_date": [date(2023, 1, 2), date(2023, 1, 3), date(2023, 1, 4)],
         "alpha_score": [1.0, 1.0, 1.0]}
    )
    benchmark = pd.DataFrame({"date": raw_prices["date"].unique(), "close": 400.0})
    corp_actions = pd.DataFrame([{
        "ticker": "AAPL",
        "ex_date": split_date,
        "action_type": "split",
        "value": 2.0,
        "known_at": datetime(2023, 1, 1, tzinfo=timezone.utc),
    }])

    handler = DataHandler(
        raw_prices, alpha_scores, benchmark, corporate_actions=corp_actions
    )
    engine = BacktestEngine()
    config = {
        "backtest": {
            "start_date": "2023-01-02",
            "end_date": str(raw_prices["date"].max()),
            "initial_capital": 10_000.0,
        },
        "portfolio": {"n_long": 1, "rebalance_frequency": "monthly"},
    }
    result = engine.run(config, handler, FillSimulator(fill_model="perfect"))

    nav_before = result.nav_series[result.nav_series.index < split_date]
    nav_after = result.nav_series[result.nav_series.index >= split_date]
    # After the initial rebalance, a buy-and-hold NAV should stay flat
    # (constant raw close on each side of the split, share count adjusted
    # for the split) -- not halve at the split boundary.
    if len(nav_before) > 1 and len(nav_after) > 0:
        last_before = nav_before.iloc[-1]
        first_after = nav_after.iloc[0]
        assert first_after == pytest.approx(last_before, rel=0.02)
