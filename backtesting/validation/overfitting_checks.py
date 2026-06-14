"""Multiple-testing and overfitting corrections for strategy evaluation.

References
----------
- Bailey & Lopez de Prado (2014): "The Deflated Sharpe Ratio: Correcting for
  Selection Bias, Backtest Overfitting, and Non-Normality."
- Benjamini & Hochberg (1995): "Controlling the False Discovery Rate."
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
import scipy.stats as stats


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_trials: int,
    n_observations: int,
    sharpe_std: float = 1.0,
    risk_free_rate: float = 0.0,
) -> float:
    """Compute the Deflated Sharpe Ratio (DSR).

    Adjusts the observed Sharpe for selection bias introduced when choosing
    the best of ``n_trials`` candidate strategies.  The DSR answers: given
    that I tested this many strategies and picked the best, what is the
    probability that the selected strategy has a true SR > 0?

    Args:
        observed_sharpe: Annualised Sharpe ratio of the selected strategy.
        n_trials: Number of strategy variants tried (including the selected one).
        n_observations: Number of daily return observations in the backtest.
        sharpe_std: Standard deviation of Sharpe ratios across trials.
            Default of 1.0 is conservative; set to observed cross-trial std
            when available.
        risk_free_rate: Assumed risk-free rate (annualised, decimal).

    Returns:
        DSR in [0, 1].  Values close to 1.0 mean the strategy is unlikely to
        be a false discovery; values close to 0.0 suggest overfitting.
    """
    if n_trials <= 0 or n_observations <= 1:
        raise ValueError("n_trials and n_observations must be positive integers > 1")

    # Expected maximum SR under the null (Gaussian approximation from BLP 2014)
    euler_mascheroni = 0.5772156649
    expected_max_sr = (
        (1 - euler_mascheroni) * stats.norm.ppf(1 - 1 / n_trials)
        + euler_mascheroni * stats.norm.ppf(1 - 1 / (n_trials * math.e))
    )
    expected_max_sr *= sharpe_std

    # Variance of the SR estimator (non-normal correction from Lo 2002)
    sr_variance = (1 + 0.5 * observed_sharpe ** 2) / (n_observations - 1)
    sr_std = math.sqrt(sr_variance)

    if sr_std <= 0:
        return 0.0

    z = (observed_sharpe - expected_max_sr) / sr_std
    return float(stats.norm.cdf(z))


def bonferroni_correction(
    p_values: Sequence[float],
    alpha: float = 0.05,
) -> list[bool]:
    """Bonferroni multiple-testing correction.

    Args:
        p_values: Sequence of p-values from individual hypothesis tests.
        alpha: Family-wise error rate.

    Returns:
        List of booleans; True means the hypothesis is rejected at the
        Bonferroni-corrected significance level.
    """
    threshold = alpha / len(p_values)
    return [p < threshold for p in p_values]


def benjamini_hochberg(
    p_values: Sequence[float],
    fdr: float = 0.05,
) -> list[bool]:
    """Benjamini-Hochberg FDR correction.

    Controls the expected false discovery rate rather than the family-wise
    error rate.  Less conservative than Bonferroni when many tests are run.

    Args:
        p_values: Sequence of p-values.
        fdr: Desired false discovery rate.

    Returns:
        List of booleans; True means the hypothesis is rejected.
    """
    n = len(p_values)
    if n == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    thresholds = [(rank + 1) / n * fdr for rank in range(n)]
    rejected_flags = [False] * n
    last_rejected = -1
    for rank, (orig_idx, p) in enumerate(indexed):
        if p <= thresholds[rank]:
            last_rejected = rank
    for rank, (orig_idx, _) in enumerate(indexed):
        if rank <= last_rejected:
            rejected_flags[orig_idx] = True
    return rejected_flags


def minimum_track_record_length(
    target_sharpe: float,
    observed_sharpe: float,
    alpha: float = 0.05,
) -> float:
    """Minimum number of monthly observations to reject SR = target at level alpha.

    Uses the asymptotic distribution of the SR estimator (Lo 2002).

    Args:
        target_sharpe: Annualised Sharpe ratio under the null (e.g. 0.0).
        observed_sharpe: Observed annualised Sharpe ratio.
        alpha: Significance level.

    Returns:
        Minimum number of months required.
    """
    z_alpha = stats.norm.ppf(1 - alpha)
    if observed_sharpe <= target_sharpe:
        return float("inf")
    # Monthly SR = annual / sqrt(12)
    monthly_sr = observed_sharpe / (12 ** 0.5)
    monthly_target = target_sharpe / (12 ** 0.5)
    delta = monthly_sr - monthly_target
    var_term = 1 + 0.5 * monthly_sr ** 2
    return float((z_alpha / delta) ** 2 * var_term + 1)
