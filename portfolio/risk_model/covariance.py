"""Covariance matrix estimators for portfolio optimization.

Provides:
- sample covariance (for reference / diagnostics)
- Ledoit-Wolf shrinkage (default — more stable for large universes)
- Oracle Approximating Shrinkage (OAS) alternative

All estimators return annualized covariance matrices in long-form DataFrames
(ticker × ticker).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import structlog
from sklearn.covariance import LedoitWolf, OAS

logger = structlog.get_logger(__name__)

_TRADING_DAYS_PER_YEAR = 252


def build_covariance(
    daily_returns: pd.DataFrame,
    method: str = "ledoit_wolf",
    min_days: int = 126,
    annualize: bool = True,
) -> pd.DataFrame:
    """Estimate a covariance matrix from daily return history.

    Parameters
    ----------
    daily_returns:
        Wide DataFrame, index=date, columns=ticker, values=daily return (decimal).
    method:
        'sample', 'ledoit_wolf' (default), or 'oas'.
    min_days:
        Minimum number of observation rows required; raises ValueError otherwise.
    annualize:
        Multiply by 252 to convert to annualized covariance.

    Returns
    -------
    Square DataFrame (ticker × ticker) with annualized covariances.
    """
    if len(daily_returns) < min_days:
        raise ValueError(
            f"Need ≥ {min_days} trading days; got {len(daily_returns)}."
        )

    # Drop columns with too many NaNs (> 20% missing)
    col_nan_frac = daily_returns.isna().mean()
    good_cols = col_nan_frac[col_nan_frac <= 0.20].index.tolist()
    dropped = len(daily_returns.columns) - len(good_cols)
    dropped_tickers = col_nan_frac[col_nan_frac > 0.20].index.tolist()
    if dropped:
        logger.warning(
            "covariance_dropped_sparse_tickers",
            n_dropped=dropped,
            tickers=dropped_tickers,
        )

    # Guard against inf returns from zero prices before dropping rows
    clean = daily_returns[good_cols].replace([float("inf"), float("-inf")], float("nan"))
    X = clean.dropna(how="any")

    n_dropped_rows = len(daily_returns) - len(X)
    if n_dropped_rows > 0:
        logger.warning(
            "covariance_dropped_rows_with_nan_or_inf",
            n_dropped=n_dropped_rows,
            remaining=len(X),
        )

    if len(X) < min_days:
        raise ValueError(
            f"After dropping NaN rows, only {len(X)} complete observations remain "
            f"(minimum {min_days})."
        )

    if method == "sample":
        cov = np.cov(X.values, rowvar=False)
    elif method == "ledoit_wolf":
        lw = LedoitWolf()
        lw.fit(X.values)
        cov = lw.covariance_
    elif method == "oas":
        oas = OAS()
        oas.fit(X.values)
        cov = oas.covariance_
    else:
        raise ValueError(f"Unknown covariance method: {method!r}")

    if annualize:
        cov = cov * _TRADING_DAYS_PER_YEAR

    result = pd.DataFrame(cov, index=good_cols, columns=good_cols)

    logger.info(
        "covariance_matrix_built",
        method=method,
        n_tickers=len(good_cols),
        n_obs=len(X),
        annualized=annualize,
    )
    return result


def returns_from_prices(
    prices: pd.DataFrame,
    as_of: date,
    lookback_days: int = 252,
) -> pd.DataFrame:
    """Compute daily returns from a price DataFrame, limited to lookback window.

    Parameters
    ----------
    prices:
        Wide DataFrame, index=date, columns=ticker, values=adjusted close.
    as_of:
        Only use price data up to and including this date (PIT safety).
    lookback_days:
        Number of trading days to include in the window.

    Returns
    -------
    Daily return DataFrame, same shape minus the first row.
    """
    mask = prices.index <= pd.Timestamp(as_of)
    window = prices.loc[mask].tail(lookback_days + 1)
    returns = window.pct_change().iloc[1:]
    # Replace inf values that arise from zero or near-zero prices (e.g., delisting)
    returns = returns.replace([float("inf"), float("-inf")], float("nan"))
    return returns
