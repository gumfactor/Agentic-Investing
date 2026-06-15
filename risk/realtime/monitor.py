"""Real-time portfolio risk monitor.

Computes risk metrics on every call to .snapshot() and fires alerts when
warning or hard thresholds are breached.  The circuit breaker is tripped
when a HARD threshold breach is detected.

Metrics computed:
- Rolling drawdown (from peak NAV)
- Historical VaR (1-day, 99%)
- Portfolio beta vs. SPY
- Maximum single-name concentration
- Sector concentration
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional

import pandas as pd
import structlog

from risk.realtime.var import conditional_var, historical_var, parametric_var, portfolio_beta

logger = structlog.get_logger(__name__)


class BreachSeverity(str, Enum):
    NONE = "none"
    WARNING = "warning"
    HARD = "hard"


@dataclass
class RiskSnapshot:
    """Point-in-time risk metrics for a portfolio."""

    as_of: date
    nav: float
    drawdown: float               # negative fraction from peak (e.g. -0.08 = -8%)
    var_1d_99: float              # 1-day 99% VaR as positive fraction
    cvar_1d_99: float             # CVaR
    portfolio_beta: float
    max_concentration: float      # largest single-name weight
    max_sector_concentration: float
    breaches: list[dict] = field(default_factory=list)  # list of {metric, severity, value, threshold}
    circuit_breaker_tripped: bool = False

    @property
    def worst_severity(self) -> BreachSeverity:
        if not self.breaches:
            return BreachSeverity.NONE
        severities = [b["severity"] for b in self.breaches]
        if BreachSeverity.HARD in severities:
            return BreachSeverity.HARD
        return BreachSeverity.WARNING


class RiskMonitor:
    """Computes risk metrics and checks thresholds.

    Parameters
    ----------
    hard_drawdown:
        Circuit-breaker drawdown threshold (negative, default -0.10).
    hard_var:
        Circuit-breaker 1-day VaR threshold (positive fraction, default 0.025).
    hard_beta:
        Circuit-breaker beta threshold (default 1.5).
    hard_concentration:
        Single-name hard threshold (default 0.05).
    warn_*:
        Warning-level thresholds (below hard thresholds).
    """

    def __init__(
        self,
        hard_drawdown: float = -0.10,
        hard_var: float = 0.025,
        hard_beta: float = 1.5,
        hard_concentration: float = 0.05,
        warn_drawdown: float = -0.05,
        warn_var: float = 0.015,
        warn_beta: float = 1.3,
        warn_concentration: float = 0.04,
    ) -> None:
        self._hard = {
            "drawdown": hard_drawdown,
            "var_1d": hard_var,
            "beta": hard_beta,
            "concentration": hard_concentration,
        }
        self._warn = {
            "drawdown": warn_drawdown,
            "var_1d": warn_var,
            "beta": warn_beta,
            "concentration": warn_concentration,
        }
        self._peak_nav: float = 0.0

    @classmethod
    def from_config(cls, cfg: dict) -> "RiskMonitor":
        """Build from the 'risk' section of settings.yaml."""
        return cls(
            hard_drawdown=cfg.get("hard_drawdown_threshold", -0.10),
            hard_var=cfg.get("hard_var_1d_threshold", 0.025),
            hard_beta=cfg.get("hard_beta_threshold", 1.5),
            hard_concentration=cfg.get("hard_concentration_threshold", 0.05),
            warn_drawdown=cfg.get("warn_drawdown_threshold", -0.05),
            warn_var=cfg.get("warn_var_1d_threshold", 0.015),
            warn_beta=cfg.get("warn_beta_threshold", 1.3),
            warn_concentration=cfg.get("warn_concentration_threshold", 0.04),
        )

    def snapshot(
        self,
        as_of: date,
        nav: float,
        weights: pd.Series,
        portfolio_returns: pd.Series,
        asset_returns: pd.DataFrame,
        benchmark_returns: pd.Series,
        covariance: pd.DataFrame | None = None,
        sector_map: dict[str, str] | None = None,
    ) -> RiskSnapshot:
        """Compute a full risk snapshot.

        Parameters
        ----------
        as_of:
            Current date.
        nav:
            Current portfolio NAV in USD.
        weights:
            Portfolio weights indexed by ticker (sum ≤ 1.0).
        portfolio_returns:
            Historical daily portfolio returns (decimal), most recent last.
        asset_returns:
            Wide daily asset returns (columns=tickers, index=date).
        benchmark_returns:
            Daily benchmark (SPY) returns.
        covariance:
            Optional pre-computed annualized covariance (for parametric VaR).
        sector_map:
            Optional {ticker: sector} for sector concentration.
        """
        # Update peak NAV
        if nav > self._peak_nav:
            self._peak_nav = nav

        # ── Drawdown ──────────────────────────────────────────────────────────
        drawdown = (nav / self._peak_nav - 1.0) if self._peak_nav > 0 else 0.0

        # ── VaR / CVaR ───────────────────────────────────────────────────────
        var_1d = 0.0
        cvar_1d = 0.0
        if len(portfolio_returns) >= 30:
            var_1d = historical_var(portfolio_returns)
            cvar_1d = conditional_var(portfolio_returns)
        elif covariance is not None and len(weights) > 0:
            var_1d = parametric_var(weights, covariance)

        # ── Beta ─────────────────────────────────────────────────────────────
        beta = portfolio_beta(weights, asset_returns, benchmark_returns)

        # ── Concentration ─────────────────────────────────────────────────────
        max_conc = float(weights.abs().max()) if len(weights) > 0 else 0.0

        # ── Sector concentration ──────────────────────────────────────────────
        max_sector_conc = 0.0
        if sector_map:
            sec_weights: dict[str, float] = {}
            for ticker, w in weights.items():
                sec = sector_map.get(str(ticker), "__other__")
                sec_weights[sec] = sec_weights.get(sec, 0.0) + float(w)
            max_sector_conc = max(sec_weights.values()) if sec_weights else 0.0

        # ── Breach detection ─────────────────────────────────────────────────
        breaches: list[dict] = []
        circuit_tripped = False

        def _check(metric: str, value: float, better_if: str = "lower") -> None:
            nonlocal circuit_tripped
            hard = self._hard[metric]
            warn = self._warn[metric]
            if better_if == "lower":
                # Lower value is better; breach when value is too HIGH
                if value >= hard:
                    sev = BreachSeverity.HARD
                elif value >= warn:
                    sev = BreachSeverity.WARNING
                else:
                    return
            else:
                # Higher value is better (e.g. drawdown closer to 0); breach when value is too LOW
                if value <= hard:
                    sev = BreachSeverity.HARD
                elif value <= warn:
                    sev = BreachSeverity.WARNING
                else:
                    return
            breaches.append({"metric": metric, "severity": sev.value, "value": value, "threshold": hard})
            if sev == BreachSeverity.HARD:
                circuit_tripped = True
            logger.warning(
                "risk_breach",
                metric=metric,
                severity=sev.value,
                value=round(value, 6),
                threshold=hard,
                date=as_of.isoformat(),
            )

        # drawdown: more negative = worse
        _check("drawdown", drawdown, better_if="higher")
        # var_1d: higher = worse (positive fraction)
        _check("var_1d", var_1d, better_if="lower")
        # beta: higher = worse
        _check("beta", beta, better_if="lower")
        # concentration: higher = worse
        _check("concentration", max_conc, better_if="lower")

        snap = RiskSnapshot(
            as_of=as_of,
            nav=nav,
            drawdown=drawdown,
            var_1d_99=var_1d,
            cvar_1d_99=cvar_1d,
            portfolio_beta=beta,
            max_concentration=max_conc,
            max_sector_concentration=max_sector_conc,
            breaches=breaches,
            circuit_breaker_tripped=circuit_tripped,
        )

        logger.info(
            "risk_snapshot",
            date=as_of.isoformat(),
            nav=round(nav, 2),
            drawdown=round(drawdown, 4),
            var_1d=round(var_1d, 4),
            beta=round(beta, 3),
            max_conc=round(max_conc, 4),
            n_breaches=len(breaches),
            circuit_tripped=circuit_tripped,
        )
        return snap
