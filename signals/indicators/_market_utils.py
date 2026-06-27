"""Shared helpers for market-relative factor modules (beta, idiosyncratic risk)."""
from __future__ import annotations
import numpy as np
import pandas as pd


def rolling_beta(
    returns: pd.DataFrame,
    benchmark_col: str,
    window: int,
    min_periods: int,
) -> pd.DataFrame:
    """Rolling OLS beta of each ticker vs a benchmark column in the same DataFrame.

    Returns a wide DataFrame of the same shape as `returns` with the benchmark
    column set to 1.0 (by definition).
    """
    if benchmark_col not in returns.columns:
        raise ValueError(f"rolling_beta requires '{benchmark_col}' in returns DataFrame")
    bench = returns[benchmark_col]
    bench_var = bench.rolling(window, min_periods=min_periods).var()
    cov = returns.rolling(window, min_periods=min_periods).cov(bench)
    beta = cov.divide(bench_var, axis=0)
    return beta


def annualised_vol(returns: pd.DataFrame, window: int, min_periods: int) -> pd.DataFrame:
    return returns.rolling(window, min_periods=min_periods).std() * np.sqrt(252)
