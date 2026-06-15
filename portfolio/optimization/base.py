"""Abstract base class for portfolio optimizers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from portfolio.risk_model.constraints import PortfolioConstraints


@dataclass
class OptimizationResult:
    """Output of any optimizer.run() call."""

    weights: pd.Series          # index=ticker, values=target weight (sum ≤ 1.0)
    objective_value: float      # optimizer objective at solution
    solver_status: str          # e.g. "optimal", "infeasible"
    diagnostics: dict           # solver-specific diagnostics


class BaseOptimizer(ABC):
    """Contract that all portfolio optimizers must satisfy."""

    @abstractmethod
    def run(
        self,
        expected_returns: pd.Series,
        covariance: pd.DataFrame,
        constraints: PortfolioConstraints | None = None,
    ) -> OptimizationResult:
        """Compute target weights.

        Parameters
        ----------
        expected_returns:
            Series indexed by ticker; annualized expected excess return (decimal).
        covariance:
            Square DataFrame (ticker × ticker); annualized covariance matrix.
        constraints:
            Optional constraint bundle. When None, only long-only / fully-invested
            constraints are applied.

        Returns
        -------
        OptimizationResult with weights summing to ≤ 1.0 (cash residual allowed).
        """

    def _align_inputs(
        self,
        expected_returns: pd.Series,
        covariance: pd.DataFrame,
    ) -> tuple[pd.Series, pd.DataFrame, list[str]]:
        """Return inputs restricted to the intersection of tickers."""
        tickers = sorted(set(expected_returns.index) & set(covariance.index) & set(covariance.columns))
        if not tickers:
            raise ValueError("No common tickers between expected_returns and covariance.")
        mu = expected_returns.loc[tickers]
        sigma = covariance.loc[tickers, tickers]
        return mu, sigma, tickers

    @staticmethod
    def _validate_covariance(sigma: pd.DataFrame) -> None:
        """Raise if covariance matrix is not positive semi-definite."""
        vals = np.linalg.eigvalsh(sigma.values)
        if vals.min() < -1e-8:
            raise ValueError(
                f"Covariance matrix has negative eigenvalue {vals.min():.6g}. "
                "Run ledoit_wolf shrinkage before optimizing."
            )
