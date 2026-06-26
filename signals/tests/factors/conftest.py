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
