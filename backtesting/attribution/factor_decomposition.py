"""Factor return decomposition.

Explains portfolio returns in terms of exposure to known risk factors
(market, sector, style) versus stock-specific alpha.

Method: OLS regression of portfolio excess returns on factor returns.
  R_portfolio_excess = beta_1*F_1 + beta_2*F_2 + ... + alpha + epsilon

Standard errors use HC3 (heteroscedasticity-robust).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import statsmodels.api as sm


@dataclass
class FactorDecompositionResult:
    """Output of a factor decomposition run."""
    factor_returns: pd.DataFrame
    factor_betas: pd.Series
    alpha: float                   # annualised daily intercept
    r_squared: float
    residuals: pd.Series
    t_stats: pd.Series
    p_values: pd.Series


def decompose_factor_returns(
    portfolio_returns: pd.Series,
    factor_returns: pd.DataFrame,
    risk_free_returns: float | pd.Series = 0.0,
    annualise_alpha: bool = True,
) -> FactorDecompositionResult:
    """Decompose portfolio returns into factor contributions + alpha.

    Args:
        portfolio_returns: Daily portfolio returns. Index = date.
        factor_returns: Daily factor returns. Index = date, columns = factor names.
        risk_free_returns: Daily risk-free rate or scalar (subtracted before regression).
        annualise_alpha: Multiply the daily intercept by 252 to express as annual rate.

    Returns:
        FactorDecompositionResult with betas, alpha, R², and residuals.
    """
    if isinstance(risk_free_returns, (int, float)):
        excess = portfolio_returns - float(risk_free_returns)
    else:
        excess = portfolio_returns - risk_free_returns.reindex(portfolio_returns.index).fillna(0.0)

    common_dates = portfolio_returns.index.intersection(factor_returns.index)
    if len(common_dates) < 10:
        raise ValueError(
            f"Insufficient overlapping dates for regression: {len(common_dates)}. Need >= 10."
        )

    y = excess.loc[common_dates].astype(float)
    X = factor_returns.loc[common_dates].astype(float)
    X_with_const = sm.add_constant(X, has_constant="add")

    model = sm.OLS(y, X_with_const).fit(cov_type="HC3")

    factor_names = [c for c in X_with_const.columns if c != "const"]
    betas = pd.Series(model.params[factor_names], name="beta")
    t_stats = pd.Series(model.tvalues[factor_names], name="t_stat")
    p_values = pd.Series(model.pvalues[factor_names], name="p_value")

    alpha = float(model.params.get("const", 0.0))
    if annualise_alpha:
        alpha *= 252

    residuals = pd.Series(model.resid, index=common_dates, name="residual")

    return FactorDecompositionResult(
        factor_returns=factor_returns.loc[common_dates],
        factor_betas=betas,
        alpha=alpha,
        r_squared=float(model.rsquared),
        residuals=residuals,
        t_stats=t_stats,
        p_values=p_values,
    )


def compute_factor_contributions(
    betas: pd.Series,
    factor_returns: pd.DataFrame,
) -> pd.DataFrame:
    """Compute the return contribution of each factor on each date.

    Args:
        betas: Factor beta estimates from FactorDecompositionResult.factor_betas.
        factor_returns: Daily factor returns (index=date, columns=factor_names).

    Returns:
        DataFrame (dates x factors) of each factor's daily contribution.
    """
    common_factors = betas.index.intersection(factor_returns.columns)
    return factor_returns[common_factors].multiply(betas[common_factors])
