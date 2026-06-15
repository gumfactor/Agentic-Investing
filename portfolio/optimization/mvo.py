"""Mean-Variance Optimizer (Markowitz / max-Sharpe) using CVXPY.

Solves the parametric QP:
    maximize   μᵀw - (γ/2) wᵀΣw
    subject to  Σ wᵢ = 1
                0 ≤ wᵢ ≤ max_position_weight   (long-only)
                wᵢ_sector ≤ max_sector_weight   (optional)

Setting gamma=0 collapses to max-return; a very large gamma collapses to
min-variance.  The default is max-Sharpe (solved via the Markowitz trick of
variable substitution).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import structlog

from portfolio.optimization.base import BaseOptimizer, OptimizationResult
from portfolio.risk_model.constraints import PortfolioConstraints

try:
    import cvxpy as cp
except ImportError as exc:  # pragma: no cover
    raise ImportError("cvxpy is required for MVO. `pip install cvxpy`") from exc

logger = structlog.get_logger(__name__)


class MVOOptimizer(BaseOptimizer):
    """Maximum-Sharpe mean-variance optimizer.

    Parameters
    ----------
    risk_aversion:
        γ in the objective μᵀw - (γ/2) wᵀΣw.  Ignored when
        mode='max_sharpe' (default).
    mode:
        'max_sharpe' (default), 'max_return', or 'min_variance'.
    solver:
        CVXPY solver string (default: CLARABEL).
    """

    def __init__(
        self,
        risk_aversion: float = 1.0,
        mode: str = "max_sharpe",
        solver: str = "CLARABEL",
    ) -> None:
        if mode not in {"max_sharpe", "max_return", "min_variance", "mean_variance"}:
            raise ValueError(f"Unknown mode: {mode!r}")
        self.risk_aversion = risk_aversion
        self.mode = mode
        self.solver = solver

    def run(
        self,
        expected_returns: pd.Series,
        covariance: pd.DataFrame,
        constraints: PortfolioConstraints | None = None,
    ) -> OptimizationResult:
        mu, sigma, tickers = self._align_inputs(expected_returns, covariance)
        self._validate_covariance(sigma)

        constraints = constraints or PortfolioConstraints()
        n = len(tickers)

        Sigma = sigma.values.astype(float)
        mu_vec = mu.values.astype(float)

        if self.mode == "max_sharpe":
            weights, obj, status = self._solve_max_sharpe(mu_vec, Sigma, n, constraints, tickers)
        elif self.mode == "min_variance":
            weights, obj, status = self._solve_min_variance(Sigma, n, constraints, tickers)
        else:
            weights, obj, status = self._solve_mean_variance(
                mu_vec, Sigma, n, constraints, self.risk_aversion, tickers
            )

        result_weights = pd.Series(weights, index=tickers, dtype=float).clip(lower=0.0)
        raw_sum = float(result_weights.sum())

        # For optimal_inaccurate: log sum-constraint violation before normalizing
        if "inaccurate" in status and abs(raw_sum - 1.0) > 0.05:
            logger.warning(
                "mvo_inaccurate_sum_violation",
                status=status,
                raw_sum=round(raw_sum, 4),
                advice="Consider tightening solver tolerance or using fallback mode.",
            )

        result_weights = result_weights / raw_sum if raw_sum > 1e-9 else result_weights

        # Check if fallback weights violate position cap
        if "fallback" in status:
            max_w = constraints.max_position_weight
            if result_weights.max() > max_w + 1e-6:
                logger.warning(
                    "mvo_fallback_violates_position_cap",
                    max_weight=round(float(result_weights.max()), 4),
                    cap=max_w,
                    n_assets=n,
                    advice="Equal-weight fallback exceeds max_position_weight. Consider increasing cap or universe size.",
                )

        logger.info(
            "mvo_optimization_complete",
            mode=self.mode,
            status=status,
            n_tickers=n,
            effective_n=int((result_weights > 1e-4).sum()),
            objective=round(float(obj), 6),
        )
        return OptimizationResult(
            weights=result_weights,
            objective_value=float(obj),
            solver_status=status,
            diagnostics={"mode": self.mode, "n_assets": n},
        )

    # ── Private solvers ──────────────────────────────────────────────────────

    def _build_base_constraints(
        self,
        w: "cp.Variable",
        n: int,
        constraints: PortfolioConstraints,
        tickers: list[str],
    ) -> list:
        """Return CVXPY constraint list shared by all modes."""
        cvx_constraints = [
            cp.sum(w) == 1,
            w >= 0.0,
            w <= constraints.max_position_weight,
        ]
        # Sector caps
        if constraints.sector_map:
            sectors: dict[str, list[int]] = {}
            for i, t in enumerate(tickers):
                sec = constraints.sector_map.get(t, "__other__")
                sectors.setdefault(sec, []).append(i)
            for sec, idxs in sectors.items():
                cvx_constraints.append(
                    cp.sum(w[idxs]) <= constraints.max_sector_weight
                )
        return cvx_constraints

    def _solve_max_sharpe(
        self,
        mu: np.ndarray,
        Sigma: np.ndarray,
        n: int,
        constraints: PortfolioConstraints,
        tickers: list[str],
    ) -> tuple[np.ndarray, float, str]:
        """Markowitz trick: y = w/κ, κ = 1/(μᵀw), then rescale."""
        y = cp.Variable(n, nonneg=True)
        kappa = cp.Variable(nonneg=True)

        portfolio_variance = cp.quad_form(y, Sigma)
        objective = cp.Minimize(portfolio_variance)

        cvx_constraints = [
            mu @ y == 1.0,
            cp.sum(y) == kappa,
            y <= constraints.max_position_weight * kappa,
            kappa >= 1e-6,
        ]
        if constraints.sector_map:
            sectors: dict[str, list[int]] = {}
            for i, t in enumerate(tickers):
                sec = constraints.sector_map.get(t, "__other__")
                sectors.setdefault(sec, []).append(i)
            for sec, idxs in sectors.items():
                cvx_constraints.append(
                    cp.sum(y[idxs]) <= constraints.max_sector_weight * kappa
                )

        prob = cp.Problem(objective, cvx_constraints)
        prob.solve(solver=self.solver, warm_start=True)

        if prob.status not in ("optimal", "optimal_inaccurate") or kappa.value is None:
            if prob.status == "infeasible":
                logger.error(
                    "mvo_max_sharpe_infeasible",
                    status=prob.status,
                    advice="All expected returns may be negative. Max-Sharpe requires at least one positive return.",
                )
            else:
                logger.warning("mvo_max_sharpe_failed", status=prob.status)
            # Fallback: equal weight
            w_val = np.ones(n) / n
            return w_val, 0.0, f"fallback:{prob.status}"

        w_val = (y.value / kappa.value).clip(0.0)
        return w_val, float(prob.value), prob.status

    def _solve_min_variance(
        self,
        Sigma: np.ndarray,
        n: int,
        constraints: PortfolioConstraints,
        tickers: list[str],
    ) -> tuple[np.ndarray, float, str]:
        w = cp.Variable(n)
        portfolio_variance = cp.quad_form(w, Sigma)
        objective = cp.Minimize(portfolio_variance)
        cvx_constraints = self._build_base_constraints(w, n, constraints, tickers)
        prob = cp.Problem(objective, cvx_constraints)
        prob.solve(solver=self.solver, warm_start=True)

        if prob.status not in ("optimal", "optimal_inaccurate") or w.value is None:
            if prob.status == "infeasible":
                logger.error(
                    "mvo_min_variance_infeasible",
                    status=prob.status,
                    advice="Min-variance problem is infeasible. Check that constraints are compatible "
                           "(e.g. max_position_weight is not too tight for the number of assets).",
                )
            else:
                logger.warning("mvo_min_variance_failed", status=prob.status)
            w_val = np.ones(n) / n
            return w_val, 0.0, f"fallback:{prob.status}"

        return w.value.clip(0.0), float(prob.value), prob.status

    def _solve_mean_variance(
        self,
        mu: np.ndarray,
        Sigma: np.ndarray,
        n: int,
        constraints: PortfolioConstraints,
        gamma: float,
        tickers: list[str],
    ) -> tuple[np.ndarray, float, str]:
        w = cp.Variable(n)
        ret = mu @ w
        risk = cp.quad_form(w, Sigma)
        objective = cp.Maximize(ret - (gamma / 2) * risk)
        cvx_constraints = self._build_base_constraints(w, n, constraints, tickers)
        prob = cp.Problem(objective, cvx_constraints)
        prob.solve(solver=self.solver, warm_start=True)

        if prob.status not in ("optimal", "optimal_inaccurate") or w.value is None:
            if prob.status == "infeasible":
                logger.error(
                    "mvo_mean_variance_infeasible",
                    status=prob.status,
                    gamma=gamma,
                    advice="All expected returns may be negative. Max-Sharpe requires at least one positive return.",
                )
            else:
                logger.warning("mvo_mean_variance_failed", status=prob.status, gamma=gamma)
            w_val = np.ones(n) / n
            return w_val, 0.0, f"fallback:{prob.status}"

        return w.value.clip(0.0), float(prob.value), prob.status
