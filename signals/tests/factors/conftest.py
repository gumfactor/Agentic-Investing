"""Shared fixtures and helpers for factor unit tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

_DEFAULT_TICKERS = ["AA", "BB", "CC", "DD", "EE"]


def make_prices(
    tickers: list[str] | None = None,
    n_days: int = 300,
    start: str = "2020-01-01",
    seed: int = 42,
) -> pd.DataFrame:
    """Random-walk prices DataFrame in long format [date, ticker, close]."""
    tickers = tickers or _DEFAULT_TICKERS
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    rows: list[dict] = []
    for i, ticker in enumerate(tickers):
        base = 50.0 + i * 20.0
        closes = np.maximum(base + np.cumsum(rng.normal(0.0, 0.3, n_days)), 1.0)
        for d, c in zip(dates, closes):
            rows.append({"date": d, "ticker": ticker, "close": float(c)})
    return pd.DataFrame(rows)


def make_fixed_prices(
    tickers: list[str] | None = None,
    close: float = 100.0,
    n_days: int = 300,
    start: str = "2020-01-01",
) -> pd.DataFrame:
    """Constant-price DataFrame — useful for sign-direction tests."""
    tickers = tickers or _DEFAULT_TICKERS
    dates = pd.bdate_range(start, periods=n_days)
    rows = [{"date": d, "ticker": t, "close": close} for d in dates for t in tickers]
    return pd.DataFrame(rows)


def make_fundamentals(
    tickers: list[str] | None = None,
    n_quarters: int = 16,
    start: str = "2016-01-01",
    seed: int = 42,
    **col_specs,
) -> pd.DataFrame:
    """
    Quarterly fundamentals in wide-metric long format [date, ticker, col...].

    col_specs values:
      scalar         – same value for every ticker × date cell
      (mean, std)    – independent normal draw per cell (always use large mean
                       and small std to keep values positive for stock-count
                       columns like shares_outstanding)
      [v0, v1, ...]  – one fixed value per ticker (constant across quarters)
    """
    tickers = tickers or _DEFAULT_TICKERS
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=n_quarters, freq="QS")
    rows: list[dict] = []
    for i, ticker in enumerate(tickers):
        for d in dates:
            row: dict = {"date": d, "ticker": ticker}
            for col, spec in col_specs.items():
                if isinstance(spec, (int, float)):
                    row[col] = float(spec)
                elif isinstance(spec, tuple) and len(spec) == 2:
                    row[col] = float(rng.normal(spec[0], spec[1]))
                elif isinstance(spec, list):
                    row[col] = float(spec[i % len(spec)])
                else:
                    row[col] = float(spec)
            rows.append(row)
    return pd.DataFrame(rows)


def _latest_scores(result: pd.DataFrame, score_col: str) -> pd.Series:
    """Return scores for the latest date, indexed by ticker."""
    latest_date = result["date"].max()
    return result[result["date"] == latest_date].set_index("ticker")[score_col]


def make_ohlc(
    tickers: list[str] | None = None,
    n_days: int = 300,
    start: str = "2020-01-01",
    seed: int = 42,
) -> pd.DataFrame:
    """OHLC DataFrame in long format [date, ticker, open, high, low, close].

    close is NOT forced to be the midpoint of high/low so CLV-based factors
    (Chaikin, A/D line, MFI) produce non-zero values.
    """
    tickers = tickers or _DEFAULT_TICKERS
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    rows: list[dict] = []
    for i, ticker in enumerate(tickers):
        base = 50.0 + i * 20.0
        price = base
        for d in dates:
            day_ret = rng.normal(0.0, 0.01)
            open_ = price * (1 + rng.normal(0.0, 0.003))
            close = max(open_ * (1 + day_ret), 0.01)
            intraday = abs(rng.normal(0.0, 0.005)) * price
            high = max(open_, close) + intraday
            low  = max(min(open_, close) - intraday, 0.01)
            rows.append({
                "date": d, "ticker": ticker,
                "open": float(open_), "high": float(high),
                "low": float(low), "close": float(close),
            })
            price = close
    return pd.DataFrame(rows)


def make_ohlcv(
    tickers: list[str] | None = None,
    n_days: int = 300,
    start: str = "2020-01-01",
    seed: int = 42,
) -> pd.DataFrame:
    """OHLCV DataFrame in long format [date, ticker, open, high, low, close, volume]."""
    tickers = tickers or _DEFAULT_TICKERS
    rng = np.random.default_rng(seed)
    ohlc = make_ohlc(tickers=tickers, n_days=n_days, start=start, seed=seed)
    dates = sorted(ohlc["date"].unique())
    vol_rows = []
    for ticker in tickers:
        for d in dates:
            vol_rows.append({"date": d, "ticker": ticker,
                             "volume": float(abs(rng.normal(1_000_000, 200_000)) + 10_000)})
    vol_df = pd.DataFrame(vol_rows)
    return ohlc.merge(vol_df, on=["date", "ticker"])


def make_volumes(
    tickers: list[str] | None = None,
    n_days: int = 300,
    start: str = "2020-01-01",
    seed: int = 42,
) -> pd.DataFrame:
    """Volume DataFrame in long format [date, ticker, volume]."""
    tickers = tickers or _DEFAULT_TICKERS
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    rows: list[dict] = []
    for ticker in tickers:
        vols = np.maximum(rng.normal(1_000_000, 200_000, n_days), 10_000)
        for d, v in zip(dates, vols):
            rows.append({"date": d, "ticker": ticker, "volume": float(v)})
    return pd.DataFrame(rows)


def make_prices_with_spy(
    tickers: list[str] | None = None,
    n_days: int = 400,
    start: str = "2020-01-01",
    seed: int = 42,
) -> pd.DataFrame:
    """Prices including SPY — required for beta and relative-strength factors."""
    tickers = list(tickers or _DEFAULT_TICKERS) + ["SPY"]
    return make_prices(tickers=tickers, n_days=n_days, start=start, seed=seed)


# ─── Pytest fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def prices_300d() -> pd.DataFrame:
    return make_prices(n_days=300)


@pytest.fixture
def prices_400d() -> pd.DataFrame:
    return make_prices(n_days=400)


@pytest.fixture
def prices_810d() -> pd.DataFrame:
    return make_prices(n_days=810)


@pytest.fixture
def ohlc_300d() -> pd.DataFrame:
    return make_ohlc(n_days=300)


@pytest.fixture
def ohlcv_300d() -> pd.DataFrame:
    return make_ohlcv(n_days=300)


@pytest.fixture
def ohlcv_400d() -> pd.DataFrame:
    return make_ohlcv(n_days=400)


@pytest.fixture
def volumes_300d() -> pd.DataFrame:
    return make_volumes(n_days=300)


@pytest.fixture
def volumes_400d() -> pd.DataFrame:
    return make_volumes(n_days=400)


@pytest.fixture
def prices_with_spy_400d() -> pd.DataFrame:
    return make_prices_with_spy(n_days=400)


@pytest.fixture
def prices_with_spy_810d() -> pd.DataFrame:
    return make_prices_with_spy(n_days=810)
