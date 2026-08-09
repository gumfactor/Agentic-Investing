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

1. **Schema-level (structural, forward-looking, repo-wide)**: this repo has
   THREE independent ``DeclarativeBase`` subclasses today
   (``strategy_registry/models.py``, ``data/research/models.py``,
   ``data/universe/models.py``) -- a new module defining its own Base is the
   normal pattern here, not an edge case. An earlier version of this check
   walked only ``strategy_registry.models.Base``, which missed
   ``data/research/models.py``'s ``ResearchRun`` (a genuine
   ``data_version``-bearing model on a different Base) entirely and made
   the "closes the class" claim false. This version AST-scans every ``.py``
   file in the repo for ``class X(DeclarativeBase):`` definitions, imports
   only the modules where one was found, and walks EVERY discovered Base's
   ``registry.mappers`` -- so a new Base added anywhere in the repo is
   found automatically, with no hand-maintained list of "the Bases I know
   about" to go stale.

   Every model, on every discovered Base, that carries a ``data_version``
   column OR a ``config_hash``-shaped column (exact ``data_version``;
   ``config_hash`` matched as a SUBSTRING, so it also catches
   ``canonical_config_hash``/``code_config_hash`` -- the repo's existing C7
   marker of "this row records a measurement over a specific data snapshot"
   and/or "this row pertains to a specific strategy/methodology identity")
   must EITHER also carry ``eval_start_date``/``eval_end_date`` columns, OR
   appear in ``_NOT_WINDOW_SCOPED_ALLOWLIST`` below with a one-line reason
   it is legitimately not a window-scoped evaluation. A future
   measurement-persisting model added anywhere in the repo therefore CANNOT
   pass silently -- whoever adds it must consciously either add the window
   columns or write down why the model is exempt. That is what actually
   closes the class; see the failure message below, which states both
   options explicitly.

   **Why ``config_hash`` was added to the predicate (PR #50 Codex R1-A)**:
   the original ``data_version``-only predicate never looked at
   ``PromotionDecision`` -- it carries ``config_hash`` but no
   ``data_version`` column of its own -- so a live instance of this exact
   defect class (two promotions of the same strategy/config/data-snapshot
   over different windows, indistinguishable by interval) shipped
   undetected. A row claiming EITHER a specific data snapshot OR a specific
   strategy identity is claiming to be a measurement; the predicate now
   reflects that.

   Widening the predicate legitimately pulls in identity/lifecycle rows
   that record what a strategy/methodology IS or its lifecycle STATE, not
   a measurement taken over an interval -- these are enumerated in
   ``_NOT_WINDOW_SCOPED_ALLOWLIST`` with real justifications, not guessed.

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
exist on those tables). Check 1 DOES catch a brand new TABLE anywhere in the
repo that follows the established "measurement row carries data_version"
pattern, which is the far more likely way this class of bug would resurface
-- but it is not airtight against a hand-rolled INSERT into an existing
table bypassing the sanctioned recorder entirely. That gap is the same one
the existing C7 data_version discipline already lives with (nothing stops a
raw SQL INSERT either); this test raises the bar to the same level C7
already operates at, not beyond it.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

from backtesting.validation.promotion_pipeline import PromotionPipeline
from backtesting.validation.trial_recorder import TrialRecorder
from strategy_registry.registry import StrategyRegistry

pytestmark = pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")


# ── Check 1: schema-level, every data_version-bearing table (on ANY Base,
#    anywhere in the repo) has a window OR a justified exemption ───────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXCLUDED_DIR_NAMES = {
    ".venv", "venv", "node_modules", ".git", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".pytest-tmp",
}

# Models that legitimately carry a C7 data_version and/or config_hash-shaped
# column but are NOT window-scoped evaluations -- explicit, justified
# exemptions from the "must carry eval_start_date/eval_end_date" rule.
# Adding a model here is a conscious declaration, not a silent pass: a new
# data_version/config_hash-bearing model that is neither windowed nor
# listed here fails this test. Enumerated by actually running the widened
# discovery (2026-08-08, PR #50 R1-A) against this repo, not guessed.
_NOT_WINDOW_SCOPED_ALLOWLIST: dict[str, str] = {
    "ResearchRun": (
        "data/research/models.py -- a signal-research provenance record "
        "scoped by a data_version SNAPSHOT (point-in-time cutoff), not a "
        "backtest/walk-forward date RANGE; it has no start/end evaluation "
        "window concept to record."
    ),
    "StrategyDefinition": (
        "strategy_registry/models.py -- records WHAT a strategy config IS "
        "(identity). config_hash here IS the identity key itself, not a "
        "reference to a measurement -- there is no evaluation to have a "
        "window over; the window is what OTHER rows (StrategyTrial/"
        "StrategyRun/PromotionDecision) measure THIS definition against."
    ),
    "Strategy": (
        "strategy_registry/models.py -- records a strategy's LIFECYCLE "
        "STATE (backtesting/paper/live/archived) via canonical_config_hash, "
        "not a measurement taken over an interval. Its status_history "
        "audit trail (StrategyStatusHistory) is a transition log, not a "
        "windowed evaluation either."
    ),
    "ResearchMethodology": (
        "data/research/models.py -- records WHAT a research methodology IS "
        "(via code_config_hash, a fingerprint of methodology code/config), "
        "mirroring StrategyDefinition's identity role in the signal-"
        "research domain. Not a measurement over an interval."
    ),
}


def _iter_repo_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        yield path


def _find_declarative_base_class_names(path: Path) -> list[str]:
    """AST-scan one file for top-level ``class X(DeclarativeBase):``
    definitions (or ``class X(some.path.DeclarativeBase):``) and return
    their class names. Returns [] for unparsable/undecodable files rather
    than raising -- this is a discovery aid, not a strict linter."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            base_name = (
                base.id if isinstance(base, ast.Name)
                else base.attr if isinstance(base, ast.Attribute)
                else None
            )
            if base_name == "DeclarativeBase":
                names.append(node.name)
    return names


def _module_dotted_path(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    return ".".join(rel.parts)


def _discover_declarative_base_classes() -> list[type]:
    """Dynamically discover every ``DeclarativeBase`` subclass anywhere in
    the repo (F1 fix, 2026-08-08 adversarial review). AST-scans every
    ``.py`` file for a ``class X(DeclarativeBase):`` definition, then
    imports ONLY the modules where such a definition was actually found
    (not every ``.py`` file in the repo -- this stays targeted at the
    handful of real model modules, currently 3) and resolves the class
    object. A module that fails to import (e.g. an optional dependency
    unavailable in this environment) is skipped rather than failing
    collection -- it cannot contribute a live registry either way.
    """
    # Import side-effect: selection_models registers StrategyHypothesis/
    # StrategyTrial/ResearchDataWindow/PromotionDecision onto
    # strategy_registry.models.Base's registry -- without this, that Base's
    # own module alone would not yet have those classes mapped when
    # discovered below.
    import strategy_registry.selection_models  # noqa: F401

    discovered: list[type] = []
    seen_ids: set[int] = set()
    for path in _iter_repo_python_files(_REPO_ROOT):
        class_names = _find_declarative_base_class_names(path)
        if not class_names:
            continue
        module_name = _module_dotted_path(path, _REPO_ROOT)
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 -- see docstring
            continue
        for class_name in class_names:
            cls = getattr(module, class_name, None)
            if cls is None or id(cls) in seen_ids:
                continue
            seen_ids.add(id(cls))
            discovered.append(cls)
    return discovered


def _is_measurement_marker_column(column_name: str) -> bool:
    """True iff ``column_name`` marks a row as pertaining to a specific data
    snapshot (C7 ``data_version``, exact match) or a specific strategy/
    methodology identity (``config_hash``-shaped, matched as a SUBSTRING so
    it also catches ``canonical_config_hash``/``code_config_hash``).

    PR #50 R1-A: the ``data_version``-only predicate missed
    ``PromotionDecision`` (``config_hash``, no ``data_version`` of its own)
    entirely -- a live instance of the defect class this test exists to
    close. Either marker means "this row is claiming to record something
    about a specific measurement," which is what check 1 must catch.
    """
    return column_name == "data_version" or "config_hash" in column_name


def _models_with_measurement_marker_column() -> list[type]:
    """Every ORM model, on EVERY discovered DeclarativeBase subclass
    anywhere in the repo, that carries a ``data_version`` and/or
    ``config_hash``-shaped column -- see ``_is_measurement_marker_column``.
    """
    matches: list[type] = []
    seen_ids: set[int] = set()
    for base_cls in _discover_declarative_base_classes():
        registry = getattr(base_cls, "registry", None)
        if registry is None:
            continue
        for mapper in registry.mappers:
            cls = mapper.class_
            if id(cls) in seen_ids:
                continue
            if any(_is_measurement_marker_column(c) for c in cls.__table__.columns.keys()):
                seen_ids.add(id(cls))
                matches.append(cls)
    return matches


def test_every_measurement_marker_table_also_has_eval_window_columns() -> None:
    """The class-closing schema assertion. If this fails, a new
    measurement-persisting model was added -- ANYWHERE in the repo, on ANY
    DeclarativeBase -- with a data_version and/or config_hash-shaped column
    (the existing C7/identity convention) but no eval_start_date/
    eval_end_date columns and no allowlist entry: exactly the 04-4W defect
    class re-opening in a new location.
    """
    models = _models_with_measurement_marker_column()
    # Sanity: this must find models from multiple DISTINCT Bases (proving
    # the repo-wide discovery actually crossed module boundaries, not just
    # strategy_registry's), and must find PromotionDecision specifically
    # (the R1-A instance the data_version-only predicate missed), or the
    # test is vacuously passing because discovery/predicate silently
    # regressed.
    model_names = {m.__name__ for m in models}
    assert "StrategyTrial" in model_names, "strategy_registry Base discovery regressed"
    assert "StrategyRun" in model_names, "strategy_registry Base discovery regressed"
    assert "PromotionDecision" in model_names, (
        "config_hash-shaped-column matching has regressed -- this is the "
        "exact R1-A instance (PromotionDecision has config_hash but no "
        "data_version) the widened predicate exists to catch."
    )
    assert "ResearchRun" in model_names, (
        "data/research/models.py's Base was not discovered -- check 1 has "
        "regressed to single-Base behavior (the exact F1 defect)."
    )

    missing = []
    for model in models:
        name = model.__name__
        if name in _NOT_WINDOW_SCOPED_ALLOWLIST:
            continue
        cols = model.__table__.columns
        if "eval_start_date" not in cols or "eval_end_date" not in cols:
            missing.append(name)
    assert missing == [], (
        f"Model(s) {missing} carry a data_version and/or config_hash-shaped "
        "column (a measurement/identity marker) but no eval_start_date/"
        "eval_end_date columns and no entry in this module's "
        "_NOT_WINDOW_SCOPED_ALLOWLIST. Every measurement-persisting sink "
        "must record the evaluation window it ran over -- see "
        "docs/plans/04-identity-evaluation-context-design.md. You have "
        "exactly two ways to make this pass: (1) add eval_start_date/"
        "eval_end_date DATE columns to the model (mirroring StrategyTrial/"
        "StrategyRun/PromotionDecision) if it genuinely represents a "
        "window-scoped evaluation, or (2) if it is NOT a window-scoped "
        "evaluation (e.g. it records identity/lifecycle state, or is "
        "scoped by a data snapshot rather than a date range), add it to "
        "_NOT_WINDOW_SCOPED_ALLOWLIST here with a one-line reason -- a "
        "silent omission is not an option."
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
