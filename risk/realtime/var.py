"""Value-at-Risk (VaR) calculator.

Implements:
- Historical VaR (non-parametric, full-history simulation)
- Parametric VaR (variance-covariance, assuming normality)
- Conditional VaR (CVaR / Expected Shortfall)

All VaR figures are expressed as positive fractions of AUM (0.025 = 2.5% loss).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_CONFIDENCE = 0.99
_DEFAULT_HORIZON_DAYS = 1


def historical_var(
    portfolio_returns: pd.Series,
    confidence: float = _DEFAULT_CONFIDENCE,
    horizon_days: int = _DEFAULT_HORIZON_DAYS,
) -> float:
    """Non-parametric historical VaR at given confidence level.

    Parameters
    ----------
    portfolio_returns:
        Daily portfolio returns (decimal), most recent last.
    confidence:
        Confidence level, e.g. 0.99 = 99%.
    horizon_days:
        Scaling horizon (square-root-of-time rule for multi-day VaR).

    Returns
    -------
    VaR as a positive fraction (e.g. 0.025 = 2.5% of AUM).
    """
    if len(portfolio_returns) < 30:
        raise ValueError(f"Need ≥ 30 returns for historical VaR; got {len(portfolio_returns)}")

    losses = -portfolio_returns.dropna()
    var_1d = float(np.quantile(losses, confidence))
    var = var_1d * np.sqrt(horizon_days)
    return max(var, 0.0)


def parametric_var(
    weights: pd.Series,
    covariance: pd.DataFrame,
    confidence: float = _DEFAULT_CONFIDENCE,
    horizon_days: int = _DEFAULT_HORIZON_DAYS,
) -> float:
    """Variance-covariance (parametric / Gaussian) VaR.

    Parameters
    ----------
    weights:
        Portfolio weights indexed by ticker (sum ≤ 1.0).
    covariance:
        Annualized covariance matrix (ticker × ticker).
    """
    from scipy.stats import norm

    tickers = [t for t in weights.index if t in covariance.index]
    w = weights.loc[tickers].values.astype(float)
    Sigma = covariance.loc[tickers, tickers].values.astype(float)

    # Daily covariance
    Sigma_daily = Sigma / 252.0
    portfolio_var_daily = float(w @ Sigma_daily @ w)
    portfolio_std_daily = np.sqrt(portfolio_var_daily)

    z = float(norm.ppf(confidence))
    var_1d = z * portfolio_std_daily
    var = var_1d * np.sqrt(horizon_days)
    return max(var, 0.0)


def conditional_var(
    portfolio_returns: pd.Series,
    confidence: float = _DEFAULT_CONFIDENCE,
    horizon_days: int = _DEFAULT_HORIZON_DAYS,
) -> float:
    """Expected Shortfall (CVaR) — average loss beyond the VaR threshold."""
    if len(portfolio_returns) < 30:
        raise ValueError(f"Need ≥ 30 returns; got {len(portfolio_returns)}")

    losses = -portfolio_returns.dropna().values
    threshold = np.quantile(losses, confidence)
    tail_losses = losses[losses >= threshold]
    cvar_1d = float(tail_losses.mean()) if len(tail_losses) > 0 else threshold
    return max(cvar_1d * np.sqrt(horizon_days), 0.0)


def portfolio_beta(
    weights: pd.Series,
    asset_returns: pd.DataFrame,
    benchmark_returns: pd.Series,
) -> float:
    """Compute weighted-average portfolio beta against a benchmark.

    Parameters
    ----------
    weights:
        Portfolio weights indexed by ticker.
    asset_returns:
        Wide daily returns (columns=tickers, index=date).
    benchmark_returns:
        Daily benchmark returns (same index as asset_returns).
    """
    tickers = [t for t in weights.index if t in asset_returns.columns]
    if not tickers:
        return 0.0

    w = weights.loc[tickers]
    ret = asset_returns[tickers].dropna(how="all")
    bench = benchmark_returns.reindex(ret.index).dropna()
    ret = ret.reindex(bench.index).dropna(how="any")

    if len(ret) < 20:
        return 1.0  # default when insufficient history

    betas = {}
    bench_var = float(np.var(bench.values, ddof=1))
    if bench_var < 1e-12:
        return 1.0
    for t in tickers:
        cov = float(np.cov(ret[t].values, bench.values, ddof=1)[0, 1])
        betas[t] = cov / bench_var

    beta_series = pd.Series(betas)
    return float((w * beta_series).sum())
