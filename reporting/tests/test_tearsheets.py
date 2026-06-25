"""Tests for the reporting/tearsheets module.

Covers:
  • metrics.py  — all pure-math functions
  • charts.py   — each chart builder returns correct type with no exception
  • tearsheet.py — TearsheetGenerator factory + both render paths
  • __init__.py  — generate_tearsheet convenience wrapper
"""
from __future__ import annotations

import math
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # must be before pyplot

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Shared synthetic data
# ---------------------------------------------------------------------------

_TICKERS = ["AAPL", "GOOG", "MSFT", "AMZN", "META"]
_N_DAYS = 252 * 2  # ~2 years of trading days


def _make_returns(n: int = _N_DAYS, seed: int = 42, mu: float = 0.0003,
                  sigma: float = 0.012) -> pd.Series:
    rng = np.random.default_rng(seed)
    vals = rng.normal(mu, sigma, n)
    start = date(2022, 1, 3)
    dates = []
    d = start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return pd.Series(vals, index=dates, name="returns")


def _make_benchmark_returns(n: int = _N_DAYS, seed: int = 99) -> pd.Series:
    rng = np.random.default_rng(seed)
    vals = rng.normal(0.0002, 0.011, n)
    start = date(2022, 1, 3)
    dates = []
    d = start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return pd.Series(vals, index=dates, name="benchmark")


def _make_positions(n: int = _N_DAYS) -> pd.DataFrame:
    start = date(2022, 1, 3)
    dates = []
    d = start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    rng = np.random.default_rng(7)
    weights = rng.dirichlet(np.ones(5), size=n) * 0.95  # slight cash
    return pd.DataFrame(weights, index=dates, columns=_TICKERS)


def _make_trades(n: int = 50) -> pd.DataFrame:
    rng = np.random.default_rng(13)
    start = date(2022, 1, 3)
    rows = []
    for i in range(n):
        d = start + timedelta(days=int(rng.integers(0, _N_DAYS)))
        while d.weekday() >= 5:
            d += timedelta(days=1)
        ticker = _TICKERS[i % 5]
        direction = "BUY" if i % 3 != 0 else "SELL"
        price = float(rng.uniform(50, 300))
        shares = float(rng.integers(10, 200))
        notional = price * shares
        commission = shares * 0.005
        mkt_impact = notional * 0.001
        rows.append({
            "date": d,
            "ticker": ticker,
            "direction": direction,
            "shares": shares,
            "fill_price": price,
            "notional": notional,
            "commission": commission,
            "market_impact": mkt_impact,
            "total_cost": commission + mkt_impact,
        })
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def _make_prices() -> pd.DataFrame:
    """Long-format prices for trade_entry_exit chart."""
    start = date(2022, 1, 3)
    dates = []
    d = start
    while len(dates) < _N_DAYS:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    rows = []
    rng = np.random.default_rng(21)
    for ticker in _TICKERS:
        price = float(rng.uniform(100, 400))
        for dt in dates:
            price *= 1 + rng.normal(0.0003, 0.012)
            rows.append({"ticker": ticker, "date": dt, "close": max(price, 1.0)})
    return pd.DataFrame(rows)


# Fixtures (module-level to avoid rebuilding for every test)
_RETURNS = _make_returns()
_BM_RETURNS = _make_benchmark_returns()
_POSITIONS = _make_positions()
_TRADES = _make_trades()
_PRICES = _make_prices()


# ---------------------------------------------------------------------------
# metrics.py tests
# ---------------------------------------------------------------------------

from reporting.tearsheets.metrics import (
    alpha,
    alpha as compute_alpha,
    annualized_return,
    annualized_volatility,
    annual_returns,
    beta,
    calmar_ratio,
    compute_metrics,
    drawdown_series,
    information_ratio,
    max_drawdown,
    monthly_returns_pivot,
    rolling_sharpe,
    sharpe_ratio,
    sortino_ratio,
)


class TestAnnualizedReturn:
    def test_positive_drift(self):
        r = pd.Series([0.001] * 252)
        assert annualized_return(r) == pytest.approx((1.001 ** 252) - 1, rel=1e-4)

    def test_flat_returns_zero(self):
        r = pd.Series([0.0] * 252)
        assert annualized_return(r) == pytest.approx(0.0, abs=1e-10)

    def test_single_element_is_nan(self):
        r = pd.Series([0.05])
        assert math.isnan(annualized_return(r))

    def test_empty_is_nan(self):
        assert math.isnan(annualized_return(pd.Series([], dtype=float)))


class TestAnnualizedVolatility:
    def test_constant_returns_zero_vol(self):
        r = pd.Series([0.001] * 252)
        assert annualized_volatility(r) == pytest.approx(0.0, abs=1e-10)

    def test_scaled_correctly(self):
        daily_std = 0.01
        r = pd.Series(np.random.default_rng(1).normal(0, daily_std, 1000))
        expected = daily_std * math.sqrt(252)
        assert annualized_volatility(r) == pytest.approx(expected, rel=0.05)


class TestSharpeRatio:
    def test_positive_sharpe(self):
        r = pd.Series([0.001] * 252)
        assert sharpe_ratio(r) > 0

    def test_negative_sharpe(self):
        r = pd.Series([-0.001] * 252)
        assert sharpe_ratio(r) < 0

    def test_zero_vol_is_nan(self):
        r = pd.Series([0.0] * 252)
        assert math.isnan(sharpe_ratio(r))


class TestSortinoRatio:
    def test_only_positive_returns(self):
        r = pd.Series([0.001] * 252)
        # No sub-zero returns → downside vol = 0 → nan
        assert math.isnan(sortino_ratio(r))

    def test_mixed_returns_not_nan(self):
        r = _RETURNS
        result = sortino_ratio(r)
        assert not math.isnan(result)

    def test_known_value(self):
        # Hand-compute: r = [+0.01, -0.01] × 3, rf = 0
        # downside_sq over all 6 periods: [0, 0.0001, 0, 0.0001, 0, 0.0001]
        # mean(downside_sq) = 0.0001/2 = 0.00005
        # downside_vol = sqrt(0.00005) * sqrt(252)
        r = pd.Series([0.01, -0.01, 0.01, -0.01, 0.01, -0.01])
        expected_dd_vol = float(np.sqrt(np.mean([0.0, 1e-4, 0.0, 1e-4, 0.0, 1e-4]))
                                * np.sqrt(252))
        expected = annualized_return(r) / expected_dd_vol
        assert sortino_ratio(r) == pytest.approx(expected, rel=1e-9)

    def test_uses_all_periods_not_just_negative(self):
        # Two series with the same negative days but different total lengths:
        # longer series has more zero-contribution days → smaller downside vol
        # → higher Sortino than the short series.
        neg_days = pd.Series([-0.01] * 10)
        padded = pd.Series([0.001] * 100 + [-0.01] * 10)
        s_short = sortino_ratio(neg_days)
        s_long = sortino_ratio(padded)
        assert not math.isnan(s_short)
        assert not math.isnan(s_long)
        # Padded series has same losses but more non-loss days → larger Sortino
        assert s_long > s_short


class TestMaxDrawdown:
    def test_monotone_up_no_drawdown(self):
        r = pd.Series([0.001] * 100)
        assert max_drawdown(r) == pytest.approx(0.0, abs=1e-9)

    def test_known_drawdown(self):
        # 50 % drawdown: price goes 100 → 50
        r = pd.Series([-0.5 / 100] * 100)   # cumulative ~-39 %
        dd = max_drawdown(r)
        assert dd < 0

    def test_negative_fraction(self):
        dd = max_drawdown(_RETURNS)
        assert dd <= 0.0
        assert dd >= -1.0

    def test_empty_is_nan(self):
        assert math.isnan(max_drawdown(pd.Series([], dtype=float)))


class TestCalmarRatio:
    def test_calmar_matches_formula(self):
        # Series with a deliberate drawdown so Calmar is well-defined
        r = pd.Series([0.002] * 100 + [-0.05] + [0.002] * 100)
        c = calmar_ratio(r)
        if not math.isnan(c):
            expected = annualized_return(r) / abs(max_drawdown(r))
            assert c == pytest.approx(expected, rel=1e-9)

    def test_positive_when_cagr_positive(self):
        r = pd.Series([0.002] * 100 + [-0.05] + [0.002] * 100)
        c = calmar_ratio(r)
        if not math.isnan(c):
            assert c > 0

    def test_zero_dd_is_nan(self):
        r = pd.Series([0.001] * 252)
        assert math.isnan(calmar_ratio(r))


class TestInformationRatio:
    def test_identical_returns_zero_active(self):
        r = _RETURNS
        ir = information_ratio(r, r)
        assert math.isnan(ir) or abs(ir) < 1e-9

    def test_reasonable_range(self):
        ir = information_ratio(_RETURNS, _BM_RETURNS)
        if not math.isnan(ir):
            assert -10 < ir < 10

    def test_zero_overlap_returns_nan(self):
        r = pd.Series([0.01, 0.02], index=[date(2020, 1, 2), date(2020, 1, 3)])
        b = pd.Series([0.01, 0.02], index=[date(2025, 1, 2), date(2025, 1, 3)])
        assert math.isnan(information_ratio(r, b))

    def test_single_overlap_returns_nan(self):
        r = pd.Series([0.01, 0.02], index=[date(2020, 1, 2), date(2020, 1, 3)])
        b = pd.Series([0.01], index=[date(2020, 1, 2)])
        assert math.isnan(information_ratio(r, b))


class TestBetaAlpha:
    def test_beta_to_itself_is_one(self):
        r = _RETURNS
        assert beta(r, r) == pytest.approx(1.0, rel=1e-6)

    def test_beta_range(self):
        b = beta(_RETURNS, _BM_RETURNS)
        assert not math.isnan(b)
        assert -5 < b < 5

    def test_alpha_to_itself_is_zero(self):
        r = _RETURNS
        b_val = beta(r, r)
        a = compute_alpha(r, r, beta_val=b_val)
        assert a == pytest.approx(0.0, abs=1e-6)

    def test_zero_overlap_beta_is_nan(self):
        r = pd.Series([0.01, 0.02], index=[date(2020, 1, 2), date(2020, 1, 3)])
        b = pd.Series([0.01, 0.02], index=[date(2025, 1, 2), date(2025, 1, 3)])
        assert math.isnan(beta(r, b))

    def test_single_overlap_beta_is_nan(self):
        r = pd.Series([0.01, 0.02], index=[date(2020, 1, 2), date(2020, 1, 3)])
        b = pd.Series([0.01], index=[date(2020, 1, 2)])
        assert math.isnan(beta(r, b))


class TestDrawdownSeries:
    def test_starts_at_zero(self):
        r = _RETURNS
        dd = drawdown_series(r)
        # At first step, cummax == cum so dd[0] == 0
        assert dd.iloc[0] == pytest.approx(0.0, abs=1e-9)

    def test_all_non_positive(self):
        dd = drawdown_series(_RETURNS)
        assert (dd <= 1e-9).all()

    def test_same_index_as_returns(self):
        dd = drawdown_series(_RETURNS)
        assert list(dd.index) == list(_RETURNS.index)


class TestRollingSharpe:
    def test_length_matches_input(self):
        rs = rolling_sharpe(_RETURNS, window=252)
        assert len(rs) == len(_RETURNS)

    def test_first_window_minus_one_are_nan(self):
        rs = rolling_sharpe(_RETURNS, window=252)
        assert rs.iloc[:251].isna().all()

    def test_not_all_nan(self):
        rs = rolling_sharpe(_RETURNS, window=60)
        assert not rs.isna().all()


class TestMonthlyReturnsPivot:
    def test_columns_are_month_labels(self):
        pivot = monthly_returns_pivot(_RETURNS)
        for col in pivot.columns:
            assert col in ["Jan","Feb","Mar","Apr","May","Jun",
                           "Jul","Aug","Sep","Oct","Nov","Dec"]

    def test_rows_are_years(self):
        pivot = monthly_returns_pivot(_RETURNS)
        for row in pivot.index:
            assert 2000 < row < 2100

    def test_empty_returns_empty_df(self):
        pivot = monthly_returns_pivot(pd.Series([], dtype=float))
        assert pivot.empty


class TestAnnualReturns:
    def test_each_year_present(self):
        ann = annual_returns(_RETURNS)
        years = {dt.year for dt in pd.to_datetime(ann.index)}
        assert 2022 in years and 2023 in years

    def test_values_in_plausible_range(self):
        ann = annual_returns(_RETURNS)
        assert ((ann > -1.0) & (ann < 5.0)).all()


class TestComputeMetrics:
    def test_keys_present(self):
        m = compute_metrics(_RETURNS, _BM_RETURNS, _TRADES, 1_000_000.0)
        for key in ["cagr", "sharpe", "sortino", "max_drawdown", "calmar",
                    "information_ratio", "beta", "alpha", "total_return",
                    "benchmark_total_return", "n_trades"]:
            assert key in m, f"Missing key: {key}"

    def test_n_trades_matches(self):
        m = compute_metrics(_RETURNS, _BM_RETURNS, _TRADES, 1_000_000.0)
        assert m["n_trades"] == len(_TRADES)

    def test_empty_trades_ok(self):
        empty_trades = pd.DataFrame(columns=["date", "ticker", "direction",
                                              "shares", "fill_price", "notional",
                                              "commission", "market_impact", "total_cost"])
        m = compute_metrics(_RETURNS, _BM_RETURNS, empty_trades, 1_000_000.0)
        assert m["n_trades"] == 0

    def test_base_metrics_preserved(self):
        m = compute_metrics(_RETURNS, _BM_RETURNS, _TRADES, 1_000_000.0,
                            base_metrics={"custom_key": 42})
        assert m["custom_key"] == 42

    def test_empty_returns_total_return_is_nan(self):
        """WEAK: empty returns series must yield NaN total_return, not 0.0."""
        empty = pd.Series([], dtype=float)
        m = compute_metrics(empty, empty, pd.DataFrame(), 1e6)
        assert math.isnan(m["total_return"])

    def test_all_nan_total_cost_is_nan(self):
        """WEAK: NaN total_cost values must yield NaN, not 0.0."""
        trades_nan_cost = _TRADES.copy()
        trades_nan_cost["total_cost"] = float("nan")
        m = compute_metrics(_RETURNS, _BM_RETURNS, trades_nan_cost, 1_000_000.0)
        assert math.isnan(m["total_transaction_cost"])


# ---------------------------------------------------------------------------
# charts.py tests
# ---------------------------------------------------------------------------

from reporting.tearsheets import charts


class TestEquityCurve:
    def test_returns_figure(self):
        fig = charts.equity_curve(_RETURNS, _BM_RETURNS)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_draws_on_provided_axes(self):
        _, ax = plt.subplots()
        result = charts.equity_curve(_RETURNS, _BM_RETURNS, ax=ax)
        assert result is None
        plt.close("all")

    def test_short_series(self):
        r = _RETURNS.iloc[:10]
        b = _BM_RETURNS.iloc[:10]
        fig = charts.equity_curve(r, b)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_with_nav_series(self):
        nav = 500_000.0 * (1 + _RETURNS).cumprod()
        fig = charts.equity_curve(_RETURNS, _BM_RETURNS, nav_series=nav)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_disjoint_benchmark_dates(self):
        # Benchmark with dates 5 years in the future — should still render
        bm_future = _BM_RETURNS.copy()
        bm_future.index = [date(d.year + 5, d.month, d.day) for d in bm_future.index]
        fig = charts.equity_curve(_RETURNS, bm_future)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_returns_no_nav_series_does_not_crash(self):
        """Codex thread 3: empty returns + no nav_series raised IndexError on cum.index[0]."""
        fig = charts.equity_curve(pd.Series([], dtype=float), pd.Series([], dtype=float))
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_benchmark_starts_at_100_no_nav_series(self):
        """WEAK: benchmark must originate at 100 on its first plotted point."""
        fig = charts.equity_curve(_RETURNS.iloc[:100], _BM_RETURNS.iloc[:100])
        ax = fig.get_axes()[0]
        bm_lines = [l for l in ax.get_lines() if l.get_label() == "Benchmark"]
        assert bm_lines, "Benchmark line not found"
        assert bm_lines[0].get_ydata()[0] == pytest.approx(100.0, rel=1e-6)
        plt.close(fig)


class TestDrawdownChart:
    def test_returns_figure(self):
        fig = charts.drawdown(_RETURNS)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_draws_on_provided_axes(self):
        _, ax = plt.subplots()
        result = charts.drawdown(_RETURNS, ax=ax)
        assert result is None
        plt.close("all")


class TestMonthlyReturnsHeatmap:
    def test_returns_figure(self):
        fig = charts.monthly_returns_heatmap(_RETURNS)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_returns_shows_placeholder(self):
        fig = charts.monthly_returns_heatmap(pd.Series([], dtype=float))
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_single_month(self):
        r = _RETURNS.iloc[:20]
        fig = charts.monthly_returns_heatmap(r)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)


class TestRollingSharpChart:
    def test_returns_figure(self):
        fig = charts.rolling_sharpe_chart(_RETURNS)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_short_series_below_window(self):
        r = _RETURNS.iloc[:30]
        fig = charts.rolling_sharpe_chart(r, window=252)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)


class TestAnnualReturnsBar:
    def test_returns_figure(self):
        fig = charts.annual_returns_bar(_RETURNS, _BM_RETURNS)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_single_year(self):
        r = _RETURNS.iloc[:100]
        b = _BM_RETURNS.iloc[:100]
        fig = charts.annual_returns_bar(r, b)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_returns(self):
        r = pd.Series([], dtype=float)
        b = pd.Series([], dtype=float)
        fig = charts.annual_returns_bar(r, b)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)


class TestReturnDistribution:
    def test_returns_figure(self):
        fig = charts.return_distribution(_RETURNS, _BM_RETURNS)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_without_benchmark(self):
        fig = charts.return_distribution(_RETURNS)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)


class TestPositionConcentration:
    def test_returns_figure(self):
        fig = charts.position_concentration(_POSITIONS)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_positions(self):
        fig = charts.position_concentration(pd.DataFrame())
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_top_n_capped_to_available(self):
        # 5 tickers, request top_n=20 — should not crash
        fig = charts.position_concentration(_POSITIONS, top_n=20)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_other_bucket_not_labelled_cash(self):
        """Codex thread 2: non-top-N holdings must appear as 'Other', not 'Cash'."""
        # Build fully-invested 6-ticker positions; request only top_n=2
        rng = np.random.default_rng(55)
        n = 50
        tickers6 = ["A", "B", "C", "D", "E", "F"]
        weights = rng.dirichlet(np.ones(6), size=n)  # rows sum to 1 — fully invested
        dates6 = list(_POSITIONS.index[:n])
        pos6 = pd.DataFrame(weights, index=dates6, columns=tickers6)
        fig = charts.position_concentration(pos6, top_n=2)
        ax = fig.get_axes()[0]
        labels = [p.get_label() for p in ax.collections]
        assert "Other" in labels, "Non-top-N holdings should be labelled 'Other'"
        assert "Cash" not in labels, "Fully-invested portfolio should not show 'Cash'"
        plt.close(fig)


class TestCumulativeCosts:
    def test_returns_figure(self):
        fig = charts.cumulative_costs(_TRADES, 1_000_000.0)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_trades(self):
        empty = pd.DataFrame(columns=["date", "ticker", "direction",
                                       "notional", "commission",
                                       "market_impact", "total_cost"])
        fig = charts.cumulative_costs(empty)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_missing_total_cost_column(self):
        t = _TRADES.drop(columns=["total_cost"])
        fig = charts.cumulative_costs(t)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)


class TestTradeEntryExit:
    def test_long_format_prices(self):
        fig = charts.trade_entry_exit("AAPL", _PRICES, _TRADES)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_missing_ticker_in_prices(self):
        fig = charts.trade_entry_exit("ZZZZ", _PRICES, _TRADES)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_wide_format_prices(self):
        wide = _PRICES.pivot(index="date", columns="ticker", values="close")
        fig = charts.trade_entry_exit("AAPL", wide, _TRADES)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_empty_trades(self):
        empty = pd.DataFrame(columns=["date", "ticker", "direction", "fill_price"])
        fig = charts.trade_entry_exit("AAPL", _PRICES, empty)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_missing_direction_column_does_not_crash(self):
        """CRASH: trades without 'direction' column must not raise KeyError."""
        trades_no_dir = _TRADES.drop(columns=["direction"])
        fig = charts.trade_entry_exit("AAPL", _PRICES, trades_no_dir)
        assert isinstance(fig, plt.Figure)
        plt.close(fig)

    def test_draws_on_provided_axes(self):
        _, ax = plt.subplots()
        result = charts.trade_entry_exit("AAPL", _PRICES, _TRADES, ax=ax)
        assert result is None
        plt.close("all")


# ---------------------------------------------------------------------------
# TearsheetGenerator tests
# ---------------------------------------------------------------------------

from reporting.tearsheets.tearsheet import TearsheetGenerator


class _FakeResult:
    """Minimal duck-type for BacktestResult."""
    def __init__(self):
        self.returns = _RETURNS
        self.benchmark_returns = _BM_RETURNS
        self.positions = _POSITIONS
        self.trades = _TRADES
        self.metrics = {}
        self.config = {
            "name": "test_strategy",
            "version": 1,
            "data_version": "v-test",
            "backtest": {"initial_capital": 500_000.0, "benchmark": "SPY"},
        }
        self.nav_series = 500_000.0 * (1 + _RETURNS).cumprod()


@pytest.fixture()
def fake_result():
    return _FakeResult()


@pytest.fixture()
def generator(fake_result):
    return TearsheetGenerator.from_backtest_result(
        fake_result, prices=_PRICES, title="Unit Test Run"
    )


class TestTearsheetGeneratorFactory:
    def test_from_backtest_result(self, fake_result):
        gen = TearsheetGenerator.from_backtest_result(fake_result)
        assert gen.title == "test_strategy"
        assert gen.initial_capital == 500_000.0

    def test_title_override(self, fake_result):
        gen = TearsheetGenerator.from_backtest_result(fake_result, title="Custom Title")
        assert gen.title == "Custom Title"

    def test_returns_series_preserved(self, fake_result):
        gen = TearsheetGenerator.from_backtest_result(fake_result)
        pd.testing.assert_series_equal(gen.returns, _RETURNS)

    def test_backtest_none_in_config_does_not_crash(self, fake_result, tmp_path):
        """GAP-1: config['backtest'] = None must not crash render_html."""
        fake_result.config["backtest"] = None
        gen = TearsheetGenerator.from_backtest_result(fake_result)
        dest = tmp_path / "tearsheet.html"
        gen.render_html(dest)
        assert dest.exists()

    def test_missing_nav_series_attribute(self, tmp_path):
        """GAP-2: result without nav_series attribute must not crash."""
        class MinimalResult:
            returns = _RETURNS
            benchmark_returns = _BM_RETURNS
            positions = _POSITIONS
            trades = _TRADES
            metrics = {}
            config = {"name": "minimal"}
            # deliberately omitting nav_series

        gen = TearsheetGenerator.from_backtest_result(MinimalResult())
        dest = tmp_path / "tearsheet.html"
        gen.render_html(dest)
        assert dest.exists()


class TestFullMetrics:
    def test_all_required_keys(self, generator):
        m = generator.full_metrics()
        for key in ["cagr", "sharpe", "max_drawdown", "n_trades"]:
            assert key in m

    def test_cached_on_second_call(self, generator):
        m1 = generator.full_metrics()
        m2 = generator.full_metrics()
        assert m1 is m2  # same dict object

    def test_n_trades_correct(self, generator):
        assert generator.full_metrics()["n_trades"] == len(_TRADES)


class TestBuildCharts:
    def test_returns_dict_of_figures(self, generator):
        figs = generator._build_charts()
        assert isinstance(figs, dict)
        assert len(figs) > 0
        for name, fig in figs.items():
            assert isinstance(fig, plt.Figure), f"{name} is not a Figure"
            plt.close(fig)

    def test_equity_curve_always_present(self, generator):
        figs = generator._build_charts()
        assert "equity_curve" in figs
        plt.close("all")

    def test_entry_exit_present_when_prices_supplied(self, generator):
        figs = generator._build_charts()
        assert "trade_entry_exit" in figs
        plt.close("all")

    def test_entry_exit_absent_without_prices(self, fake_result):
        gen = TearsheetGenerator.from_backtest_result(fake_result, prices=None)
        figs = gen._build_charts()
        assert "trade_entry_exit" not in figs
        plt.close("all")


class TestRenderHtml:
    def test_creates_file(self, generator, tmp_path):
        dest = tmp_path / "tearsheet.html"
        result = generator.render_html(dest)
        assert result == dest
        assert dest.exists()

    def test_file_is_non_empty(self, generator, tmp_path):
        dest = tmp_path / "tearsheet.html"
        generator.render_html(dest)
        assert dest.stat().st_size > 10_000  # expect > 10 kB with embedded charts

    def test_html_structure(self, generator, tmp_path):
        dest = tmp_path / "tearsheet.html"
        generator.render_html(dest)
        html = dest.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in html
        assert "Unit Test Run" in html
        assert "data:image/png;base64," in html

    def test_metrics_in_html(self, generator, tmp_path):
        dest = tmp_path / "tearsheet.html"
        generator.render_html(dest)
        html = dest.read_text(encoding="utf-8")
        assert "Sharpe Ratio" in html
        assert "Max Drawdown" in html
        assert "CAGR" in html

    def test_creates_parent_dirs(self, generator, tmp_path):
        dest = tmp_path / "a" / "b" / "tearsheet.html"
        generator.render_html(dest)
        assert dest.exists()

    def test_no_nan_strings_in_html(self, generator, tmp_path):
        dest = tmp_path / "tearsheet.html"
        generator.render_html(dest)
        html = dest.read_text(encoding="utf-8")
        # Our formatters should convert NaN to "—", not the string "nan"
        assert "nan" not in html.lower().split("</html>")[0].split("data:image")[0]

    def test_render_html_empty_positions(self, tmp_path):
        """GAP-4: empty positions DataFrame must not crash render_html."""
        gen = TearsheetGenerator(
            returns=_RETURNS,
            benchmark_returns=_BM_RETURNS,
            positions=pd.DataFrame(),
            trades=_TRADES,
            metrics={},
            config={},
        )
        dest = tmp_path / "tearsheet.html"
        gen.render_html(dest)
        assert dest.exists()

    def test_render_html_then_render_png_dir(self, generator, tmp_path):
        """GAP-5: calling render_html then render_png_dir on same generator must both succeed."""
        html_path = tmp_path / "report.html"
        generator.render_html(html_path)
        assert html_path.exists()
        png_dir = tmp_path / "charts"
        paths = generator.render_png_dir(png_dir)
        assert len(paths) > 0
        for p in paths:
            assert p.exists() and p.suffix == ".png"

    def test_no_figure_leak_after_render_html(self, generator, tmp_path):
        """EDGE: render_html must close all figures it opens."""
        plt.close("all")
        before = len(plt.get_fignums())
        generator.render_html(tmp_path / "tearsheet.html")
        after = len(plt.get_fignums())
        assert after == before

    def test_html_injection_escaped(self, tmp_path):
        """EDGE: XSS vectors in title/config must be escaped in HTML output."""
        gen = TearsheetGenerator(
            returns=_RETURNS,
            benchmark_returns=_BM_RETURNS,
            positions=_POSITIONS,
            trades=_TRADES,
            metrics={},
            config={"name": "<script>alert(1)</script>", "backtest": {"benchmark": "<img>"}},
            title="<b>hack</b>",
        )
        dest = tmp_path / "tearsheet.html"
        gen.render_html(dest)
        content = dest.read_text(encoding="utf-8")
        assert "<script>alert" not in content
        assert "<b>hack</b>" not in content
        assert "&lt;script&gt;" in content or "alert(1)" not in content


class TestRenderPngDir:
    def test_creates_png_files(self, generator, tmp_path):
        paths = generator.render_png_dir(tmp_path / "charts")
        assert len(paths) > 0
        for p in paths:
            assert p.exists()
            assert p.suffix == ".png"

    def test_creates_output_dir(self, generator, tmp_path):
        out = tmp_path / "nested" / "charts"
        assert not out.exists()
        generator.render_png_dir(out)
        assert out.exists()

    def test_equity_curve_png_created(self, generator, tmp_path):
        out = tmp_path / "charts"
        paths = generator.render_png_dir(out)
        names = [p.name for p in paths]
        assert "equity_curve.png" in names


# ---------------------------------------------------------------------------
# __init__.py convenience wrapper
# ---------------------------------------------------------------------------

from reporting.tearsheets import generate_tearsheet


class TestGenerateTearsheet:
    def test_html_output(self, fake_result, tmp_path):
        dest = tmp_path / "report.html"
        result = generate_tearsheet(fake_result, dest, title="Wrapper Test")
        assert result == dest
        assert dest.exists()
        html = dest.read_text(encoding="utf-8")
        assert "Wrapper Test" in html

    def test_directory_output_generates_pngs(self, fake_result, tmp_path):
        dest = tmp_path / "charts"
        result = generate_tearsheet(fake_result, dest)
        assert result == dest
        assert dest.exists()
        # Equity curve PNG must be present
        assert (dest / "equity_curve.png").exists()

    def test_directory_output_creates_multiple_charts(self, fake_result, tmp_path):
        dest = tmp_path / "charts"
        generate_tearsheet(fake_result, dest)
        pngs = list(dest.glob("*.png"))
        assert len(pngs) >= 5  # at minimum: equity, drawdown, monthly, rolling, annual
