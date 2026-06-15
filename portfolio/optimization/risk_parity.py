"""Risk-Parity (Equal Risk Contribution) optimizer.

Each asset contributes the same fraction of total portfolio variance.

Solved via the Spinu (2013) convex reformulation:
    minimize  0.5 * yᵀΣy - bᵀ log(y)    (unconstrained in y)
    then normalize: w = y / sum(y)

where b is the risk budget vector (uniform = 1/n for equal risk).
This formulation is strictly convex and far more numerically stable than
the pairwise-variance formulation, with no equality constraints to complicate
the search.  CLARABEL or ECOS handle it reliably.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import structlog
from scipy.optimize import minimize

from portfolio.optimization.base import BaseOptimizer, OptimizationResult
from portfolio.risk_model.constraints import PortfolioConstraints

try:
    import cvxpy as cp
    _CVXPY_AVAILABLE = True
except ImportError:
    _CVXPY_AVAILABLE = False

logger = structlog.get_logger(__name__)

_MAX_ITER = 2000
_TOL = 1e-10


class RiskParityOptimizer(BaseOptimizer):
    """Equal-risk-contribution (ERC / risk-parity) optimizer.

    Parameters
    ----------
    budget:
        Risk budget fractions (must sum to 1.0).  None = equal budget.
    solver:
        CVXPY solver string.  Falls back to scipy L-BFGS-B if CVXPY unavailable.
    """

    def __init__(
        self,
        budget: np.ndarray | None = None,
        solver: str = "CLARABEL",
    ) -> None:
        self.budget = budget
        self.solver = solver

    def run(
        self,
        expected_returns: pd.Series,
        covariance: pd.DataFrame,
        constraints: PortfolioConstraints | None = None,
    ) -> OptimizationResult:
        mu, sigma, tickers = self._align_inputs(expected_returns, covariance)
        self._validate_covariance(sigma)

        n = len(tickers)
        Sigma = sigma.values.astype(float)

        budget = self.budget
        if budget is None:
            budget = np.ones(n) / n
        elif len(budget) != n:
            raise ValueError(f"budget length {len(budget)} != n_tickers {n}")

        constraints_obj = constraints or PortfolioConstraints()

        if _CVXPY_AVAILABLE:
            w_val, obj, status, n_iter = self._solve_cvxpy(Sigma, budget, n)
        else:
            w_val, obj, status, n_iter = self._solve_scipy(Sigma, budget, n)

        # Normalize to sum=1 FIRST (Spinu y-values are unnormalized)
        w_sum = w_val.sum()
        if w_sum > 1e-9:
            w_val = w_val / w_sum

        # Post-hoc position cap (approximate; changes ERC property slightly)
        max_w = constraints_obj.max_position_weight
        if w_val.max() > max_w + 1e-6:
            w_val = np.clip(w_val, 0.0, max_w)
            w_sum = w_val.sum()
            if w_sum > 1e-9:
                w_val = w_val / w_sum
            status = f"{status}+clipped"

        weights = pd.Series(w_val, index=tickers, dtype=float)

        logger.info(
            "risk_parity_optimization_complete",
            status=status,
            n_tickers=n,
            objective=round(float(obj), 10),
        )
        return OptimizationResult(
            weights=weights,
            objective_value=float(obj),
            solver_status=status,
            diagnostics={"solver": self.solver if _CVXPY_AVAILABLE else "scipy", "n_iter": n_iter},
        )

    # ── Private solvers ──────────────────────────────────────────────────────

    def _solve_cvxpy(
        self, Sigma: np.ndarray, budget: np.ndarray, n: int
    ) -> tuple[np.ndarray, float, str, int]:
        """Spinu (2013) convex formulation via CVXPY."""
        y = cp.Variable(n, nonneg=True)
        # minimize  0.5 * y^T Sigma y - b^T log(y)
        objective = cp.Minimize(0.5 * cp.quad_form(y, Sigma) - budget @ cp.log(y))
        prob = cp.Problem(objective)
        prob.solve(solver=self.solver, warm_start=True)

        if prob.status not in ("optimal", "optimal_inaccurate") or y.value is None:
            logger.warning("risk_parity_cvxpy_failed", status=prob.status)
            # Fall back to scipy
            return self._solve_scipy(Sigma, budget, n)

        y_val = y.value.clip(1e-10, None)
        return y_val, float(prob.value), prob.status, 0

    def _solve_scipy(
        self, Sigma: np.ndarray, budget: np.ndarray, n: int
    ) -> tuple[np.ndarray, float, str, int]:
        """Fallback: L-BFGS-B on the Spinu unconstrained objective (in log space)."""
        # Optimize in log-space for better conditioning: y_i = exp(z_i)
        def obj(z: np.ndarray) -> float:
            y = np.exp(z)
            return 0.5 * float(y @ Sigma @ y) - float(budget @ z)

        def grad(z: np.ndarray) -> np.ndarray:
            y = np.exp(z)
            return y * (Sigma @ y) - budget

        z0 = np.zeros(n)
        result = minimize(obj, z0, jac=grad, method="L-BFGS-B",
                         options={"maxiter": _MAX_ITER, "ftol": _TOL, "gtol": 1e-8})

        status = "optimal" if result.success else f"approx:{result.message[:40]}"
        y_val = np.exp(result.x).clip(1e-10, None)
        return y_val, float(result.fun), status, result.nit
