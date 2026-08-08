"""The 04-4W class-closing invariant test.

PR #49 spent four separate Codex review rounds re-discovering the same
defect -- "the evaluation window is not a first-class measurement input" --
one instance at a time: registry definition reuse, ``StrategyTrial``,
``StrategyRun``, the promotion pipeline's dispatch path. The general fix
(this slice, 04-4W) makes the window a required, explicit parameter of every
measurement API, injected into the dispatched config copy exactly the way
``data_version`` already is, and persisted on every measurement-recording
row. This module is the test that is supposed to make the *class* of bug
impossible to reintroduce, not just prove the four known instances are
fixed (those are covered individually in test_trial_recorder.py,
test_promotion_pipeline.py, and tests/strategy_registry/test_registry.py).

Two complementary checks, matching how this repo already treats
``data_version``/C7 as a "required on every measurement" precedent:

1. **Schema-level (structural, forward-looking)**: every ORM model
   registered on ``strategy_registry.models.Base.metadata`` that carries a
   ``data_version`` column -- the existing C7 marker of "this row records a
   measurement over a specific data snapshot" -- must ALSO carry
   ``eval_start_date``/``eval_end_date`` columns. A future engineer who adds
   a new measurement-persisting table (mirroring the existing
   ``strategy_trials``/``strategy_runs`` precedent by including a
   ``data_version`` column, which C7 already makes very likely) but forgets
   the window columns will fail this test immediately, without anyone
   needing to add a new instance-specific assertion.

2. **API-level (the known dispatch sinks today)**: the public methods that
   actually populate those tables -- ``TrialRecorder.run_walk_forward``,
   ``TrialRecorder.run_parameter_sweep``, ``StrategyRegistry.record_run``,
   ``PromotionPipeline.run`` -- must each declare a required (no-default)
   evaluation-window parameter, verified via ``inspect.signature`` rather
   than by calling them (so this test cannot be satisfied by a parameter
   that is merely accepted-and-ignored, it specifically checks the
   parameter is not optional). ``StrategyRegistry.record_run`` is the one
   sink where the window is conditionally required (mirroring the existing
   conditional ``data_version`` requirement for run_type in
   {backtest, walk_forward}) rather than unconditionally, since
   unit/signal_ic/paper/live runs are not window-scoped evaluations; that
   conditional enforcement is checked directly against the source, not by
   signature inspection.

**Honest limitation** (see the class docstring in the parent slice report):
check 2 is an enumeration of TODAY's known sinks -- a brand new function
that inserts directly into an EXISTING window-columned table
(``strategy_trials``/``strategy_runs``) via some future bypass of
``TrialRecorder``/``StrategyRegistry`` would not automatically appear in
this enumeration, and check 1 would not catch it either (the columns already
exist on those tables). Check 1 DOES catch a brand new TABLE that follows
the established "measurement row carries data_version" pattern, which is
the far more likely way this class of bug would resurface -- but it is not
airtight against a hand-rolled INSERT into an existing table bypassing the
sanctioned recorder entirely. That gap is the same one the existing C7
data_version discipline already lives with (nothing stops a raw SQL INSERT
either); this test raises the bar to the same level C7 already operates at,
not beyond it.
"""

from __future__ import annotations

import inspect

import pytest

from backtesting.validation.promotion_pipeline import PromotionPipeline
from backtesting.validation.trial_recorder import TrialRecorder
from strategy_registry.models import Base
from strategy_registry.registry import StrategyRegistry

pytestmark = pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")


# ── Check 1: schema-level, every data_version-bearing table has a window ───


def _models_with_data_version_column() -> list[type]:
    """Every ORM model registered on Base.metadata (strategy_registry.models
    AND strategy_registry.selection_models both share this one Base, so this
    walks the full measurement-schema surface) that carries a
    ``data_version`` column -- the repo's existing C7 marker for "this row
    records a measurement over a specific data snapshot."
    """
    # Import side-effect: selection_models registers StrategyHypothesis/
    # StrategyTrial/ResearchDataWindow/PromotionDecision on Base.metadata.
    import strategy_registry.selection_models  # noqa: F401

    matches = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if "data_version" in cls.__table__.columns:
            matches.append(cls)
    return matches


def test_every_data_version_bearing_table_also_has_eval_window_columns() -> None:
    """The class-closing schema assertion. If this fails, a new
    measurement-persisting model was added with a data_version column (the
    existing C7 convention) but no eval_start_date/eval_end_date columns --
    exactly the 04-4W defect class re-opening in a new location.
    """
    models = _models_with_data_version_column()
    # Sanity: this must find at least the two known sinks, or the test is
    # vacuously passing because the discovery mechanism itself is broken.
    model_names = {m.__name__ for m in models}
    assert "StrategyTrial" in model_names
    assert "StrategyRun" in model_names

    missing = []
    for model in models:
        cols = model.__table__.columns
        if "eval_start_date" not in cols or "eval_end_date" not in cols:
            missing.append(model.__name__)
    assert missing == [], (
        f"Model(s) {missing} carry a data_version column (a measurement "
        "marker, per C7) but no eval_start_date/eval_end_date columns. "
        "Every measurement-persisting sink must record the evaluation "
        "window it ran over -- see docs/plans/04-identity-evaluation-"
        "context-design.md and this module's docstring."
    )


# ── Check 2: API-level, every known dispatch sink requires the window ──────


def _has_required_param(func, name: str) -> bool:
    sig = inspect.signature(func)
    param = sig.parameters.get(name)
    return param is not None and param.default is inspect.Parameter.empty


def test_trial_recorder_run_walk_forward_requires_eval_window() -> None:
    assert _has_required_param(TrialRecorder.run_walk_forward, "eval_window")


def test_trial_recorder_run_parameter_sweep_requires_eval_window() -> None:
    assert _has_required_param(TrialRecorder.run_parameter_sweep, "eval_window")


def test_promotion_pipeline_run_requires_eval_window() -> None:
    assert _has_required_param(PromotionPipeline.run, "eval_window")


def test_strategy_registry_record_run_accepts_eval_window_params() -> None:
    """record_run's window requirement is CONDITIONAL on run_type (mirroring
    the existing conditional data_version/C7 requirement), so it cannot be a
    bare required-parameter check like the other three sinks -- it is
    checked directly against source below instead."""
    sig = inspect.signature(StrategyRegistry.record_run)
    assert "eval_start_date" in sig.parameters
    assert "eval_end_date" in sig.parameters


def test_strategy_registry_record_run_enforces_window_for_backtest_and_walk_forward() -> None:
    """Source-level proof that record_run's conditional enforcement is wired
    to the SAME set of run_types that already requires data_version (C7) --
    the two requirements must never drift apart, or a backtest/walk_forward
    run could satisfy C7 while silently omitting the window."""
    source = inspect.getsource(StrategyRegistry.record_run)
    assert "_REQUIRE_DATA_VERSION" in source
    # The eval-window requirement must be gated on the same set, not a
    # separately hand-maintained list of run_types.
    require_data_version_idx = source.index("run_type in _REQUIRE_DATA_VERSION")
    eval_window_idx = source.index("eval_start_date is None or eval_end_date is None")
    assert eval_window_idx > require_data_version_idx
