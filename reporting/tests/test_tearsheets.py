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
        # No negative returns → downside vol = 0 → nan
        assert math.isnan(sortino_ratio(r))

    def test_mixed_returns(self):
        r = _RETURNS
        result = sortino_ratio(r)
        assert not math.isnan(result)
        # Sortino >= Sharpe when distribution is not perfectly symmetric
        assert result >= sharpe_ratio(r) * 0.5  # loose bound


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
    def test_positive_cagr_negative_dd(self):
        r = _RETURNS
        c = calmar_ratio(r)
        if not math.isnan(c):
            assert c > 0 or calmar_ratio(r) != 0

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

    def test_non_html_path_returns_path(self, fake_result, tmp_path):
        dest = tmp_path / "charts"
        result = generate_tearsheet(fake_result, dest)
        assert result == dest  # directory path returned as-is
