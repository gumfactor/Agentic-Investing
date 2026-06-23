"""Performance metrics for tearsheet generation.

All functions operate on pandas Series with date (or DatetimeIndex) indices
and return Python floats or DataFrames.  No side effects, no I/O.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

_TRADING_DAYS_PER_YEAR = 252
_MONTH_LABELS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_dt(s: pd.Series) -> pd.Series:
    """Return series with DatetimeIndex (required for resample)."""
    if isinstance(s.index, pd.DatetimeIndex):
        return s
    return s.copy().set_axis(pd.to_datetime(s.index))


# ---------------------------------------------------------------------------
# Core statistics
# ---------------------------------------------------------------------------

def annualized_return(returns: pd.Series) -> float:
    """Compound annual growth rate from a daily returns series."""
    if len(returns) < 2:
        return float("nan")
    n_years = len(returns) / _TRADING_DAYS_PER_YEAR
    total = float((1 + returns).prod())
    if total <= 0 or n_years <= 0:
        return float("nan")
    return float(total ** (1.0 / n_years) - 1)


def annualized_volatility(returns: pd.Series) -> float:
    if len(returns) < 2:
        return float("nan")
    return float(returns.std(ddof=1) * np.sqrt(_TRADING_DAYS_PER_YEAR))


def sharpe_ratio(returns: pd.Series, risk_free_rate: float = 0.0) -> float:
    vol = annualized_volatility(returns)
    if not vol or np.isnan(vol):
        return float("nan")
    return float((annualized_return(returns) - risk_free_rate) / vol)


def sortino_ratio(returns: pd.Series, risk_free_rate: float = 0.0) -> float:
    downside = returns[returns < 0]
    if len(downside) < 2:
        return float("nan")
    downside_vol = float(downside.std(ddof=1) * np.sqrt(_TRADING_DAYS_PER_YEAR))
    if not downside_vol:
        return float("nan")
    return float((annualized_return(returns) - risk_free_rate) / downside_vol)


def max_drawdown(returns: pd.Series) -> float:
    """Maximum peak-to-trough drawdown (negative fraction)."""
    if returns.empty:
        return float("nan")
    cum = (1 + returns).cumprod()
    dd = (cum - cum.cummax()) / cum.cummax()
    return float(dd.min())


def calmar_ratio(returns: pd.Series) -> float:
    cagr = annualized_return(returns)
    mdd = max_drawdown(returns)
    if np.isnan(mdd) or mdd == 0:
        return float("nan")
    return float(cagr / abs(mdd))


def information_ratio(returns: pd.Series, benchmark_returns: pd.Series) -> float:
    r, b = returns.align(benchmark_returns, join="inner")
    active = r - b
    if len(active) < 2 or active.std(ddof=1) == 0:
        return float("nan")
    return float(active.mean() / active.std(ddof=1) * np.sqrt(_TRADING_DAYS_PER_YEAR))


def beta(returns: pd.Series, benchmark_returns: pd.Series) -> float:
    r, b = returns.align(benchmark_returns, join="inner")
    if len(r) < 2 or b.var(ddof=1) == 0:
        return float("nan")
    return float(r.cov(b) / b.var(ddof=1))


def alpha(
    returns: pd.Series,
    benchmark_returns: pd.Series,
    beta_val: Optional[float] = None,
) -> float:
    """Jensen's alpha (annualised, risk-free = 0)."""
    if beta_val is None:
        beta_val = beta(returns, benchmark_returns)
    if np.isnan(beta_val):
        return float("nan")
    return float(annualized_return(returns) - beta_val * annualized_return(benchmark_returns))


# ---------------------------------------------------------------------------
# Derived series
# ---------------------------------------------------------------------------

def drawdown_series(returns: pd.Series) -> pd.Series:
    """Underwater drawdown series (≤ 0) aligned to returns.index."""
    cum = (1 + returns).cumprod()
    return (cum - cum.cummax()) / cum.cummax()


def rolling_sharpe(returns: pd.Series, window: int = _TRADING_DAYS_PER_YEAR) -> pd.Series:
    """Rolling annualised Sharpe ratio (risk-free = 0)."""
    roll_mean = returns.rolling(window).mean()
    roll_std = returns.rolling(window).std(ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(
            roll_std > 0,
            roll_mean / roll_std * np.sqrt(_TRADING_DAYS_PER_YEAR),
            np.nan,
        )
    return pd.Series(rs, index=returns.index)


def monthly_returns_pivot(returns: pd.Series) -> pd.DataFrame:
    """Year × month DataFrame of monthly returns.

    Columns are abbreviated month names (Jan … Dec); rows are calendar years.
    Missing months are NaN.
    """
    s = _to_dt(returns)
    monthly = (1 + s).resample("ME").prod() - 1
    if monthly.empty:
        return pd.DataFrame()
    df = pd.DataFrame({
        "year": monthly.index.year,
        "month": monthly.index.month,
        "return": monthly.values,
    })
    pivot = df.pivot(index="year", columns="month", values="return")
    # Rename numeric month columns to labels, keeping only those present
    pivot.columns = [_MONTH_LABELS[int(c) - 1] for c in pivot.columns]
    return pivot


def annual_returns(returns: pd.Series) -> pd.Series:
    """Annual returns with DatetimeIndex."""
    s = _to_dt(returns)
    return (1 + s).resample("YE").prod() - 1


# ---------------------------------------------------------------------------
# Full metrics bundle
# ---------------------------------------------------------------------------

def compute_metrics(
    returns: pd.Series,
    benchmark_returns: pd.Series,
    trades: pd.DataFrame,
    initial_capital: float,
    base_metrics: Optional[dict] = None,
) -> dict:
    """Compute the full tearsheet metrics dictionary.

    Parameters
    ----------
    returns:          Daily strategy returns (decimal).
    benchmark_returns: Daily benchmark returns (decimal), may have a different
                      date range — will be inner-aligned as needed.
    trades:           Trade log DataFrame from BacktestResult.trades.
    initial_capital:  Starting capital for cost normalisation.
    base_metrics:     Optional dict to merge (e.g. BacktestResult.metrics).
                      Values here are overwritten by freshly computed values.
    """
    m: dict = dict(base_metrics or {})

    b_val = beta(returns, benchmark_returns)
    aligned_bm = benchmark_returns.reindex(returns.index).fillna(0.0)

    m.update({
        "total_return": float((1 + returns).prod() - 1),
        "benchmark_total_return": float((1 + aligned_bm).prod() - 1),
        "cagr": annualized_return(returns),
        "annualized_volatility": annualized_volatility(returns),
        "sharpe": sharpe_ratio(returns),
        "sortino": sortino_ratio(returns),
        "max_drawdown": max_drawdown(returns),
        "calmar": calmar_ratio(returns),
        "information_ratio": information_ratio(returns, benchmark_returns),
        "beta": b_val,
        "alpha": alpha(returns, benchmark_returns, b_val),
        "n_trading_days": len(returns),
    })

    # Monthly distribution
    s = _to_dt(returns)
    monthly_r = (1 + s).resample("ME").prod() - 1
    if not monthly_r.empty:
        m["best_month"] = float(monthly_r.max())
        m["worst_month"] = float(monthly_r.min())
        m["positive_months_pct"] = float((monthly_r > 0).mean())

    # Trade statistics
    if not trades.empty:
        m["n_trades"] = int(len(trades))
        if "total_cost" in trades.columns:
            m["total_transaction_cost"] = float(trades["total_cost"].sum())
        if "notional" in trades.columns and "total_cost" in trades.columns:
            denom = trades["notional"].replace(0, np.nan)
            m["avg_trade_cost_bps"] = float(
                (trades["total_cost"] / denom * 10_000).mean()
            )
    else:
        m.setdefault("n_trades", 0)

    return m
