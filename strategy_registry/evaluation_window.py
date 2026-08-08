"""``EvaluationWindow`` -- the evaluation window as a first-class,
per-measurement input (Roadmap Gate 04, slice 04-4W).

Per ``docs/plans/04-identity-evaluation-context-design.md`` (operator
decision, 2026-08-07, Option 1), a strategy's identity (``config_hash``,
``strategy_registry.fingerprint``) deliberately excludes
``backtest.start_date``/``backtest.end_date`` -- the window a measurement is
taken over is evaluation context, not part of what the strategy IS. That
means the window can no longer be treated as "just another config field":
every measurement API must accept it explicitly and every measurement-
persisting sink must record it, or the window silently reverts to being
sourced from whatever a caller happened to leave in a stored config -- the
exact defect class PR #49 spent four review rounds re-discovering one
instance at a time (registry reuse, ``StrategyTrial``, ``StrategyRun``, the
promotion pipeline's dispatch path).

This module is intentionally tiny and dependency-free (stdlib only) so it can
be imported by both sides of the boundary without creating a cycle:

- ``strategy_registry`` (identity/registration) already cannot import from
  ``backtesting`` (verified: no ``backtesting`` import appears anywhere under
  ``strategy_registry/``).
- ``backtesting.validation`` (measurement: ``TrialRecorder``,
  ``PromotionPipeline``) already imports from ``strategy_registry``
  (``fingerprint``, ``models``, ``registry``, ``selection_models``).

Placing ``EvaluationWindow`` in ``strategy_registry`` -- the side with zero
inbound dependency from the other -- lets both import it directly with no new
edge in the dependency graph and no third shared package to introduce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EvaluationWindow:
    """An explicit, immutable evaluation date range for one measurement.

    Required, positional-independent construction: ``EvaluationWindow(start=
    ..., end=...)``. Validated eagerly in ``__post_init__`` so an invalid
    window can never be constructed and threaded through by mistake --
    every consumer that receives one may assume ``start <= end`` without
    re-checking.
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(
                f"EvaluationWindow.start ({self.start}) must be <= "
                f"EvaluationWindow.end ({self.end})."
            )
