"""Tests for PromotionPipeline (Gate 04 slice 04-4,
docs/plans/04-strategy-selection-protocol-design.md §4.4, §7 row 04-4).

Mirrors the DB-testing convention established in test_trial_recorder.py:
file-based SQLite (so separate engine instances -- PromotionPipeline,
StrategyRegistry, HypothesisRegistry, TrialRecorder each open their own --
see the same committed data) with ``PRAGMA foreign_keys=ON``. No real
backtest runs: WalkForwardValidator/ParameterSweeper are ``unittest.mock``
doubles, bootstrap_stress/deflated_sharpe_ratio are injected as plain
callables (real deflated_sharpe_ratio is cheap/deterministic and used
directly in most tests; bootstrap_stress is stubbed for verdict control).
No MLflow/broker touched -- ``backtest_logger`` defaults to a MagicMock
spec'd against BacktestLogger.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import structlog.testing
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from backtesting.experiment_tracking.mlflow_logger import BacktestLogger
from backtesting.validation.bootstrap_stress import BootstrapStressResult
from backtesting.validation.parameter_sensitivity import (
    ParameterSensitivityResult,
    ParameterSensitivityRow,
    ParameterSweeper,
)
from backtesting.validation.promotion_pipeline import (
    HoldoutConfirmationNotSupportedError,
    MissingParameterGridError,
    PromotionPipeline,
    RESIDUAL_BUG_ACKNOWLEDGEMENTS,
    _sanitize_metrics,
)
from backtesting.validation.walk_forward import WalkForwardFold, WalkForwardResult, WalkForwardValidator
from strategy_registry.evaluation_window import EvaluationWindow
from strategy_registry.fingerprint import hash_config
from strategy_registry.hypothesis import HypothesisRegistry
from strategy_registry.models import Base, Strategy, StrategyDefinition
from strategy_registry.selection_models import (
    PromotionDecision,
    StrategyHypothesis,
    StrategyTrial,
)


DATA_VERSION = "a" * 64

# All _config()/_seed_definition() helpers below default to this same
# 2022-01-01..2022-12-31 window (04-4W: eval_window is now a required,
# explicit PromotionPipeline.run() argument, never read from the seeded
# StrategyDefinition.config's dates).
DEFAULT_EVAL_WINDOW = EvaluationWindow(start=date(2022, 1, 1), end=date(2022, 12, 31))


# ── Fixtures / helpers ───────────────────────────────────────────────────────


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'promotion_pipeline.db'}"


def _raw_engine(db_url: str):
    engine = create_engine(db_url, future=True)
    if db_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragma(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    Base.metadata.create_all(engine)
    return engine


def _config(strategy_id: str = "v1_test_strategy") -> dict:
    return {
        "name": strategy_id,
        "version": 1,
        "backtest": {"start_date": "2022-01-01", "end_date": "2022-12-31"},
        # Round-5 fix: PromotionPipeline.run() now recomputes each sweep
        # row's EFFECTIVE applied config (via _apply_params) to dedupe on,
        # using this same seeded config as the base -- so every dot-path
        # key any fixture's ParameterSensitivityRow.params uses ("portfolio
        # .n_long", "universe") must already exist here for _apply_params/
        # _set_nested to resolve it, exactly as a real strategy config
        # would already define every tunable a param_grid can override.
        "portfolio": {"n_long": 10},
        "universe": {"source": "sp500"},
    }


def _seed_definition(db_url: str, strategy_id: str = "v1_test_strategy", config: dict | None = None) -> str:
    config = config or _config(strategy_id)
    config_hash = hash_config(config)
    engine = _raw_engine(db_url)
    with Session(engine) as session:
        session.add(
            StrategyDefinition(
                strategy_id=strategy_id,
                config_hash=config_hash,
                name=strategy_id,
                version=1,
                config=config,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    return config_hash


_UNSET = object()


def _seed_hypothesis(
    db_url: str,
    strategy_id: str = "v1_test_strategy",
    param_grid: dict | None = _UNSET,
) -> int:
    registry = HypothesisRegistry(db_url)
    hyp = registry.register_hypothesis(
        strategy_id=strategy_id,
        hypothesis_text="Does momentum window sensitivity hold up OOS?",
        param_grid_json=(
            {"portfolio.n_long": [10, 20]} if param_grid is _UNSET else param_grid
        ),
    )
    return hyp.id


def _fold(is_sharpe: float, n_trades: int, fold_number: int = 1) -> WalkForwardFold:
    trades = pd.DataFrame({"pnl": [1.0] * n_trades}) if n_trades else pd.DataFrame()
    return WalkForwardFold(
        fold_number=fold_number,
        train_start=date(2022, 1, 1),
        train_end=date(2022, 6, 30),
        test_start=date(2022, 7, 1),
        test_end=date(2022, 12, 31),
        in_sample=SimpleNamespace(metrics={"sharpe": is_sharpe}),
        out_of_sample=SimpleNamespace(trades=trades),
    )


def _oos_returns(n: int = 260) -> pd.Series:
    idx = pd.date_range("2022-07-01", periods=n, freq="D")
    return pd.Series(np.full(n, 0.0005), index=idx)


def _wf_result(
    *,
    sharpe: float = 1.1,
    max_dd: float = -0.10,
    is_sharpe: float = 1.0,
    trade_count: int = 40,
    n_folds: int = 2,
) -> WalkForwardResult:
    per_fold_trades = trade_count // n_folds
    folds = [_fold(is_sharpe, per_fold_trades, i + 1) for i in range(n_folds)]
    return WalkForwardResult(
        folds=folds,
        oos_returns=_oos_returns(),
        oos_metrics={"sharpe": sharpe, "max_drawdown": max_dd, "cagr": 0.12},
        config={},
    )


def _sweep_result(
    verdict: str = "robust",
    configs_tested: int = 6,
    n_finite_rows: int | None = None,
    duplicate_params: bool = False,
) -> ParameterSensitivityResult:
    """R1-B (PR #50): PromotionPipeline now recomputes the finite-variant
    count directly from ``rows`` (not just ``configs_tested``) to gate the
    sensitivity verdict -- see MIN_SENSITIVITY_SWEEP_VARIANTS. Defaults to
    populating ``rows`` with ``configs_tested`` finite-Sharpe entries (a
    well-powered sweep) unless ``n_finite_rows`` overrides that count, for
    tests exercising the underpowered path specifically.

    Round-2 P1 fix: ``duplicate_params=True`` gives every row the SAME
    ``params`` dict (as ``_resolve_frozen_grid`` permits when a param_grid
    list contains repeated values), for tests exercising the dedupe-by-
    normalized-params path -- distinct from ``n_finite_rows``, which still
    produces DISTINCT params per row.

    Round-6 fix: PromotionPipeline now recomputes verdict/mean/std/
    positive_fraction itself from the (deduped) rows via
    summarize_variants, rather than trusting this object's own verdict=
    field -- so when ``verdict="curve_fit"`` is requested, the per-row
    ``oos_sharpe`` values alternate between a high and a deeply negative
    value (wide dispersion, std > the 0.5 default max_sharpe_std) so a
    REAL recomputation over these rows also independently lands on
    curve_fit, not just this object's cosmetic label."""
    n_rows = configs_tested if n_finite_rows is None else n_finite_rows
    sharpes = (
        [2.0 if i % 2 == 0 else -2.0 for i in range(n_rows)]
        if verdict == "curve_fit"
        else [0.9] * n_rows
    )
    rows = [
        ParameterSensitivityRow(
            params={"portfolio.n_long": 10} if duplicate_params else {"portfolio.n_long": 10 + i},
            oos_sharpe=sharpes[i],
            oos_max_drawdown=-0.10,
            trade_count=40,
            avg_is_sharpe=1.0,
        )
        for i in range(n_rows)
    ]
    return ParameterSensitivityResult(
        base_config_name="v1_test_strategy",
        param_grid={"portfolio.n_long": [10, 20]},
        configs_tested=configs_tested,
        rows=rows,
        mean_oos_sharpe=0.9,
        std_oos_sharpe=0.1,
        positive_fraction=1.0,
        curve_fit_flag=(verdict == "curve_fit"),
        verdict=verdict,
    )


def _stress_result(verdict: str = "solid") -> BootstrapStressResult:
    return BootstrapStressResult(
        n_reshuffles=500,
        drawdown_p5=-0.20,
        drawdown_p50=-0.10,
        drawdown_p95=-0.05,
        worst_case_drawdown=-0.22 if verdict == "solid" else -0.60,
        fragile=(verdict == "fragile"),
        verdict=verdict,
    )


def _mock_validator(result: WalkForwardResult | None = None) -> MagicMock:
    mock = MagicMock(spec=WalkForwardValidator)
    mock.run.return_value = result if result is not None else _wf_result()
    return mock


def _mock_sweeper(result: ParameterSensitivityResult | None = None) -> MagicMock:
    mock = MagicMock(spec=ParameterSweeper)
    mock.sweep.return_value = result if result is not None else _sweep_result()
    return mock


def _make_pipeline(
    db_url: str,
    *,
    validator_result: WalkForwardResult | None = None,
    sweep_result: ParameterSensitivityResult | None = None,
    stress_result: BootstrapStressResult | None = None,
    backtest_logger=None,
    **kwargs,
) -> PromotionPipeline:
    stress = stress_result if stress_result is not None else _stress_result("solid")
    return PromotionPipeline(
        db_url,
        DATA_VERSION,
        walk_forward_validator=_mock_validator(validator_result),
        parameter_sweeper=_mock_sweeper(sweep_result),
        bootstrap_stress_fn=lambda *a, **kw: stress,
        backtest_logger=backtest_logger,
        **kwargs,
    )


def _independent_n_trials(db_url: str, strategy_id: str) -> int:
    """Independently re-derive n_trials directly from strategy_trials rows,
    not by calling PromotionPipeline._compute_n_trials, so the acceptance
    test genuinely cross-checks the pipeline's own computation."""
    engine = _raw_engine(db_url)
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(StrategyTrial).where(
                    StrategyTrial.strategy_id == strategy_id,
                    StrategyTrial.window == "train_oos",
                )
            )
        )
    total = 0
    for row in rows:
        if row.run_type == "walk_forward":
            total += 1
        elif row.run_type == "parameter_sweep_variant":
            total += int((row.metrics_json or {}).get("configs_tested", 1))
    return total


# ── Happy path: overall_passed True, n_trials_used matches independent count ──


def test_promotion_pipeline_pass_produces_matching_promotion_decision(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(sharpe=1.1, max_dd=-0.10, is_sharpe=1.0, trade_count=40),
        sweep_result=_sweep_result(verdict="robust", configs_tested=6),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.funnel_passed is True
    assert result.sensitivity_verdict == "robust"
    assert result.stress_verdict == "solid"
    assert result.overall_passed is True
    assert result.promotion_decision_id is not None

    expected_n_trials = _independent_n_trials(db_url, "v1_test_strategy")
    assert expected_n_trials == 1 + 6  # 1 walk_forward + 6 sweep configs_tested
    assert result.n_trials_used == expected_n_trials

    # Persisted row matches the returned PromotionResult.
    engine = _raw_engine(db_url)
    with Session(engine) as session:
        row = session.get(PromotionDecision, result.promotion_decision_id)
        assert row.overall_passed is True
        assert row.n_trials_used == expected_n_trials
        assert row.funnel_passed is True
        assert row.sensitivity_verdict == "robust"
        assert row.stress_verdict == "solid"


# ── Single-stage failures flip overall_passed and are attributed ────────────


def test_funnel_failure_flips_overall_passed_and_is_attributed(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    # sharpe below min_oos_sharpe (0.5) fails the funnel; sweep/stress pass.
    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(sharpe=0.1, max_dd=-0.10, is_sharpe=0.1, trade_count=40),
        sweep_result=_sweep_result(verdict="robust"),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.funnel_passed is False
    assert result.overall_passed is False
    assert "min_oos_sharpe" in result.evidence_json["funnel"]["verdict"]
    failed_gate_names = [
        g["name"] for g in result.evidence_json["funnel"]["gates"] if not g["passed"]
    ]
    assert "min_oos_sharpe" in failed_gate_names


def test_sensitivity_curve_fit_flips_overall_passed_and_is_attributed(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="curve_fit", configs_tested=4),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.funnel_passed is True
    assert result.sensitivity_verdict == "curve_fit"
    assert result.overall_passed is False
    assert result.evidence_json["sensitivity"]["verdict"] == "curve_fit"


# ── R1-B (PR #50 Codex round-1 P1): an underpowered sweep must not clear the
#    gate even when ParameterSweeper itself labels it "robust" ────────────────


def test_single_variant_robust_sweep_does_not_clear_overall_passed(db_url: str) -> None:
    """A single-combination grid (or one variant that happens to survive)
    trivially satisfies ParameterSweeper's positive_fraction >= 0.5
    threshold and skips its std-dispersion gate (n_valid > 1 guard), so
    ParameterSweeper itself reports verdict='robust' with zero statistical
    power. PromotionPipeline must recompute the finite-variant count and
    refuse to pass overall_passed on that basis -- this is the exact defect
    class (curve-fit strategy clearing the funnel) R1-B closes."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        # ParameterSweeper itself would compute verdict="robust" here (one
        # finite, positive Sharpe -> positive_fraction=1.0 >= 0.5, std gate
        # skipped) -- the sweep result asserts that verdict explicitly to
        # prove the PromotionPipeline-level override is what closes the gate,
        # not ParameterSweeper disagreeing with itself.
        sweep_result=_sweep_result(verdict="robust", configs_tested=1, n_finite_rows=1),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.funnel_passed is True
    assert result.stress_verdict == "solid"
    assert result.sensitivity_verdict == "robust"  # ParameterSweeper's own (uncorrected) verdict
    assert result.overall_passed is False  # but PromotionPipeline refuses to trust it
    assert result.evidence_json["sensitivity"]["n_finite_variants"] == 1
    assert result.evidence_json["sensitivity"]["underpowered"] is True
    assert result.evidence_json["sensitivity"]["min_required_variants"] == 3


def test_mostly_failed_sweep_with_one_survivor_does_not_clear_overall_passed(
    db_url: str,
) -> None:
    """A grid where all but one variant failed (n_finite=1 out of a larger
    configs_tested) is the other shape of the same defect -- configs_tested
    alone would look like a real sweep ran, but only one row actually
    produced a usable result."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust", configs_tested=8, n_finite_rows=1),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.overall_passed is False
    assert result.evidence_json["sensitivity"]["configs_tested"] == 8
    assert result.evidence_json["sensitivity"]["n_finite_variants"] == 1
    assert result.evidence_json["sensitivity"]["underpowered"] is True


def test_exactly_min_finite_variants_clears_the_underpowered_gate(db_url: str) -> None:
    """The boundary case: exactly MIN_SENSITIVITY_SWEEP_VARIANTS (3) finite
    variants must NOT be flagged underpowered -- pins the threshold as
    inclusive, not exclusive."""
    from backtesting.validation.promotion_pipeline import MIN_SENSITIVITY_SWEEP_VARIANTS

    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(
            verdict="robust",
            configs_tested=MIN_SENSITIVITY_SWEEP_VARIANTS,
            n_finite_rows=MIN_SENSITIVITY_SWEEP_VARIANTS,
        ),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.evidence_json["sensitivity"]["underpowered"] is False
    assert result.overall_passed is True


# ── Round-2 (PR #50 Codex P1): duplicate param combinations must not count
#    as distinct variants ────────────────────────────────────────────────


def test_duplicate_param_variants_do_not_clear_the_underpowered_gate(db_url: str) -> None:
    """_resolve_frozen_grid does not reject a param_grid list with repeated
    values (e.g. {"portfolio.n_long": [10, 10, 10]}), so ParameterSweeper can
    produce MIN_SENSITIVITY_SWEEP_VARIANTS-many finite rows that all share the
    SAME params -- zero actual parameter-sensitivity testing occurred.
    PromotionPipeline must dedupe by normalized params before comparing
    against the threshold and refuse to pass overall_passed on that basis."""
    from backtesting.validation.promotion_pipeline import MIN_SENSITIVITY_SWEEP_VARIANTS

    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(
            verdict="robust",
            configs_tested=MIN_SENSITIVITY_SWEEP_VARIANTS,
            n_finite_rows=MIN_SENSITIVITY_SWEEP_VARIANTS,
            duplicate_params=True,
        ),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.sensitivity_verdict == "robust"  # ParameterSweeper's own (uncorrected) verdict
    assert result.evidence_json["sensitivity"]["n_finite_variants"] == 1  # deduped, not 3
    assert result.evidence_json["sensitivity"]["underpowered"] is True
    assert result.overall_passed is False


def test_distinct_params_at_min_threshold_still_clears_after_dedupe(db_url: str) -> None:
    """Sanity counterpart to the duplicate-params test: MIN_SENSITIVITY_SWEEP_VARIANTS
    genuinely DISTINCT finite variants still dedupe down to the full count and
    clear the gate -- the dedupe fix must not under-count real diversity."""
    from backtesting.validation.promotion_pipeline import MIN_SENSITIVITY_SWEEP_VARIANTS

    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(
            verdict="robust",
            configs_tested=MIN_SENSITIVITY_SWEEP_VARIANTS,
            n_finite_rows=MIN_SENSITIVITY_SWEEP_VARIANTS,
            duplicate_params=False,
        ),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.evidence_json["sensitivity"]["n_finite_variants"] == MIN_SENSITIVITY_SWEEP_VARIANTS
    assert result.evidence_json["sensitivity"]["underpowered"] is False
    assert result.overall_passed is True


def test_nested_dict_param_values_do_not_crash_the_dedupe(db_url: str) -> None:
    """Round-3 (PR #50 Codex P2): _resolve_frozen_grid only requires each
    param_grid value to be a non-empty list/tuple of candidates -- it does
    not require the candidates themselves to be scalar. A candidate can be
    a list/dict (e.g. {"universe": [{"source": "sp500"}, ...]}) and still
    pass 04-3's strict-JSON param_grid validation (JSON-serializable, not
    scalar). A dedupe key built from tuple(sorted(row.params.items()))
    would raise TypeError: unhashable type the moment such a row is put in
    a set. The json.dumps(..., sort_keys=True) dedupe key must handle this
    without raising, and must still distinguish genuinely different nested
    values."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    rows = [
        ParameterSensitivityRow(
            params={"universe": {"source": "sp500", "as_of": "2022-01-01"}},
            oos_sharpe=0.9,
            oos_max_drawdown=-0.10,
            trade_count=40,
            avg_is_sharpe=1.0,
        ),
        ParameterSensitivityRow(
            params={"universe": {"source": "sp500", "as_of": "2022-01-01"}},
            oos_sharpe=0.8,
            oos_max_drawdown=-0.10,
            trade_count=40,
            avg_is_sharpe=1.0,
        ),
        ParameterSensitivityRow(
            params={"universe": {"source": "nasdaq100", "as_of": "2022-01-01"}},
            oos_sharpe=0.7,
            oos_max_drawdown=-0.10,
            trade_count=40,
            avg_is_sharpe=1.0,
        ),
    ]
    sweep_result = ParameterSensitivityResult(
        base_config_name="v1_test_strategy",
        param_grid={"universe": [{"source": "sp500"}, {"source": "nasdaq100"}]},
        configs_tested=3,
        rows=rows,
        mean_oos_sharpe=0.8,
        std_oos_sharpe=0.1,
        positive_fraction=1.0,
        curve_fit_flag=False,
        verdict="robust",
    )

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=sweep_result,
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    # 3 rows, but only 2 distinct normalized param sets -- the two
    # identical sp500 rows collapse to one.
    assert result.evidence_json["sensitivity"]["n_finite_variants"] == 2


def test_ancestor_descendant_grid_paths_collapse_to_same_effective_config(db_url: str) -> None:
    """Round-5 (PR #50 Codex P1): distinct row.params dicts can still
    resolve to the IDENTICAL effective backtest config when the grid has
    overlapping ancestor/descendant dot-paths -- _reject_window_override_
    keys only blocks paths that are ancestors of backtest.start_date/
    end_date specifically, so a grid combining e.g. "portfolio" (replaces
    the whole subtree) with "portfolio.n_long" (a leaf within it) is
    otherwise perfectly legal. _apply_params applies a row's params dict
    entries in insertion order, so whichever key comes later always wins
    for any key it also touches. Two rows whose "portfolio" candidate
    differs but whose later "portfolio.n_long" override lands on the SAME
    value therefore backtest the identical config -- json.dumps(row.params)
    dedup (round-3) would still count them as 2 distinct variants; the
    round-5 fix (dedupe on the fully-applied config) must collapse them to
    1."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    rows = [
        ParameterSensitivityRow(
            # "portfolio" replaces the whole subtree with {"n_long": 999},
            # then "portfolio.n_long" (applied after, same insertion
            # order as the dict literal) overrides just that leaf to 10 --
            # net effect: portfolio == {"n_long": 10}.
            params={"portfolio": {"n_long": 999}, "portfolio.n_long": 10},
            oos_sharpe=0.9,
            oos_max_drawdown=-0.10,
            trade_count=40,
            avg_is_sharpe=1.0,
        ),
        ParameterSensitivityRow(
            # Different "portfolio" candidate (888 vs 999 above), but the
            # SAME final "portfolio.n_long" override (10) -- net effect is
            # IDENTICAL to the row above: portfolio == {"n_long": 10}.
            params={"portfolio": {"n_long": 888}, "portfolio.n_long": 10},
            oos_sharpe=0.8,
            oos_max_drawdown=-0.10,
            trade_count=40,
            avg_is_sharpe=1.0,
        ),
        ParameterSensitivityRow(
            # Genuinely different final value (20, not 10) -- must remain
            # a distinct variant.
            params={"portfolio": {"n_long": 777}, "portfolio.n_long": 20},
            oos_sharpe=0.7,
            oos_max_drawdown=-0.10,
            trade_count=40,
            avg_is_sharpe=1.0,
        ),
    ]
    sweep_result = ParameterSensitivityResult(
        base_config_name="v1_test_strategy",
        param_grid={"portfolio": [{"n_long": 999}, {"n_long": 888}, {"n_long": 777}], "portfolio.n_long": [10, 20]},
        configs_tested=3,
        rows=rows,
        mean_oos_sharpe=0.8,
        std_oos_sharpe=0.1,
        positive_fraction=1.0,
        curve_fit_flag=False,
        verdict="robust",
    )

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=sweep_result,
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    # 3 rows, 3 DISTINCT raw params dicts, but only 2 distinct EFFECTIVE
    # configs -- the first two rows backtest the identical config despite
    # having different raw params.
    assert result.evidence_json["sensitivity"]["n_finite_variants"] == 2


def test_duplicated_winner_does_not_mask_two_distinct_losers(db_url: str) -> None:
    """Round-6 (PR #50 Codex P1): the round-5 dedupe fix only corrected the
    finite-variant COUNT gate -- sensitivity_verdict/positive_fraction/
    std_oos_sharpe themselves were still sourced from sensitivity_result,
    i.e. computed by ParameterSweeper over every RAW row. A grid with 100
    copies of one profitable configuration and 2 distinct losing
    configurations has 3 distinct effective configs (clears
    MIN_SENSITIVITY_SWEEP_VARIANTS), but the duplicated winner would still
    dominate ParameterSweeper's own positive_fraction/std -- 2 of the 3
    real configs lose, yet the sweep would report "robust". PromotionPipeline
    must recompute the verdict from ONE row per distinct effective config,
    not merely check the count."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    winner_rows = [
        ParameterSensitivityRow(
            params={"portfolio.n_long": 10},
            oos_sharpe=2.0,
            oos_max_drawdown=-0.05,
            trade_count=40,
            avg_is_sharpe=1.0,
        )
        for _ in range(100)
    ]
    loser_rows = [
        ParameterSensitivityRow(
            params={"portfolio.n_long": 20},
            oos_sharpe=-0.5,
            oos_max_drawdown=-0.30,
            trade_count=40,
            avg_is_sharpe=-0.2,
        ),
        ParameterSensitivityRow(
            params={"portfolio.n_long": 30},
            oos_sharpe=-0.8,
            oos_max_drawdown=-0.35,
            trade_count=40,
            avg_is_sharpe=-0.3,
        ),
    ]
    rows = winner_rows + loser_rows
    sweep_result = ParameterSensitivityResult(
        base_config_name="v1_test_strategy",
        param_grid={"portfolio.n_long": [10, 20, 30]},
        configs_tested=len(rows),
        rows=rows,
        # ParameterSweeper's OWN (uncorrected) report over all 102 raw
        # rows: positive_fraction dominated by the 100 duplicated winners.
        mean_oos_sharpe=1.933,
        std_oos_sharpe=0.35,
        positive_fraction=100 / 102,
        curve_fit_flag=False,
        verdict="robust",
    )

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=sweep_result,
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    # 3 distinct effective configs -- clears MIN_SENSITIVITY_SWEEP_VARIANTS.
    assert result.evidence_json["sensitivity"]["n_finite_variants"] == 3
    assert result.evidence_json["sensitivity"]["underpowered"] is False
    # ParameterSweeper's own (uncorrected) verdict still says "robust" --
    # the fixture asserts this explicitly to prove the PromotionPipeline-
    # level override is what closes the gate, not ParameterSweeper
    # disagreeing with itself.
    assert result.evidence_json["sensitivity"]["raw_verdict"] == "robust"
    # But the AUTHORITATIVE verdict, recomputed over one row per distinct
    # effective config (positive_fraction = 1/3, well below the 0.5
    # default), must be curve_fit -- and overall_passed must be False.
    assert result.sensitivity_verdict == "curve_fit"
    assert result.evidence_json["sensitivity"]["verdict"] == "curve_fit"
    assert result.evidence_json["sensitivity"]["positive_fraction"] == pytest.approx(1 / 3)
    assert result.overall_passed is False


def test_stress_fragile_flips_overall_passed_and_is_attributed(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust"),
        stress_result=_stress_result("fragile"),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.funnel_passed is True
    assert result.sensitivity_verdict == "robust"
    assert result.stress_verdict == "fragile"
    assert result.overall_passed is False
    assert result.evidence_json["stress"]["verdict"] == "fragile"


# ── DSR is informational only ────────────────────────────────────────────────


def test_low_dsr_does_not_flip_overall_passed(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust"),
        deflated_sharpe_fn=lambda **kwargs: 0.01,  # deliberately terrible DSR
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.dsr_value == pytest.approx(0.01)
    assert result.overall_passed is True  # every hard gate still passed
    assert result.evidence_json["overfitting"]["dsr_value"] == pytest.approx(0.01)
    assert result.evidence_json["overfitting"]["dsr_informational_only"] is True


def test_dsr_is_computed_and_recorded(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(db_url)
    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.dsr_value is not None
    assert 0.0 <= result.dsr_value <= 1.0


# ── FIX 2: n_trials < 2 is insufficient for a meaningful DSR ────────────────


def test_compute_dsr_returns_none_and_skips_deflated_sharpe_fn_when_n_trials_is_one(
    db_url: str,
) -> None:
    """n_trials == 1 must degenerate to dsr_value=None rather than calling
    deflated_sharpe_ratio, which would otherwise return a spurious 1.0 for
    ANY observed Sharpe (norm.ppf(1 - 1/1) == norm.ppf(0) == -inf)."""
    mock_dsr_fn = MagicMock(return_value=0.5)
    pipeline = _make_pipeline(db_url, deflated_sharpe_fn=mock_dsr_fn)

    dsr = pipeline._compute_dsr(observed_sharpe=-3.0, n_trials=1, n_observations=250)

    assert dsr is None
    mock_dsr_fn.assert_not_called()


def test_compute_dsr_computes_normally_when_n_trials_is_two(db_url: str) -> None:
    """n_trials == 2 is the minimum sufficient count and must still compute
    a real DSR via deflated_sharpe_fn."""
    mock_dsr_fn = MagicMock(return_value=0.42)
    pipeline = _make_pipeline(db_url, deflated_sharpe_fn=mock_dsr_fn)

    dsr = pipeline._compute_dsr(observed_sharpe=1.1, n_trials=2, n_observations=250)

    assert dsr == pytest.approx(0.42)
    mock_dsr_fn.assert_called_once_with(
        observed_sharpe=1.1,
        n_trials=2,
        n_observations=250,
        sharpe_std=pipeline._sharpe_std,
        risk_free_rate=pipeline._risk_free_rate,
    )


# NOTE: an end-to-end "holdout promotion with n_trials == 1" test used to
# live here. It relied on a separate holdout-dated StrategyDefinition plus
# a hand-seeded ResearchDataWindow to sidestep the fact that a promoted
# strategy's config_hash includes its backtest.start_date/end_date -- i.e.
# it exercised the exact flawed workaround FIX 3 (see
# HoldoutConfirmationNotSupportedError below) gates closed. It has been
# removed; the n_trials < 2 DSR-skip behavior it exercised is still fully
# covered at the unit level by
# test_compute_dsr_returns_none_and_skips_deflated_sharpe_fn_when_n_trials_is_one
# above, which calls PromotionPipeline._compute_dsr directly and needs no
# holdout machinery.


# ── Fail closed on missing/unlinked/empty hypothesis grid ──────────────────────


def test_missing_hypothesis_id_fails_closed(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    validator = _mock_validator()
    pipeline = _make_pipeline(db_url)
    # Swap in a spy validator so we can prove it was never dispatched.
    pipeline._wf_validator = validator

    with pytest.raises(MissingParameterGridError):
        pipeline.run("v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=None)

    validator.run.assert_not_called()


def test_hypothesis_with_no_param_grid_fails_closed(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url, param_grid=None)
    validator = _mock_validator()
    pipeline = _make_pipeline(db_url)
    pipeline._wf_validator = validator

    with pytest.raises(MissingParameterGridError):
        pipeline.run(
            "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
        )

    validator.run.assert_not_called()


def test_hypothesis_belonging_to_different_strategy_fails_closed(db_url: str) -> None:
    config_hash = _seed_definition(db_url, strategy_id="v1_test_strategy")
    other_hyp_id = _seed_hypothesis(db_url, strategy_id="v1_other_strategy")
    pipeline = _make_pipeline(db_url)

    with pytest.raises(MissingParameterGridError):
        pipeline.run(
            "v1_test_strategy",
            config_hash,
            data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW,
            hypothesis_id=other_hyp_id,
        )


# ── FIX 1: a structurally-nonempty grid can still have unusable values ─────────


def test_grid_with_empty_candidate_list_fails_closed_before_walk_forward(db_url: str) -> None:
    """{"k": []} is structurally a non-empty dict but ParameterSweeper would
    discover zero combinations for that key -- must fail BEFORE the
    (expensive) walk-forward is dispatched, not after."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url, param_grid={"portfolio.n_long": []})
    validator = _mock_validator()
    pipeline = _make_pipeline(db_url)
    pipeline._wf_validator = validator

    with pytest.raises(MissingParameterGridError):
        pipeline.run(
            "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
        )

    validator.run.assert_not_called()


def test_grid_with_scalar_value_fails_closed_before_walk_forward(db_url: str) -> None:
    """{"k": 10} (a bare scalar, not a candidate list) must also fail
    closed before the walk-forward is dispatched."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url, param_grid={"portfolio.n_long": 10})
    validator = _mock_validator()
    pipeline = _make_pipeline(db_url)
    pipeline._wf_validator = validator

    with pytest.raises(MissingParameterGridError):
        pipeline.run(
            "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
        )

    validator.run.assert_not_called()


def test_grid_with_string_value_fails_closed_before_walk_forward(db_url: str) -> None:
    """A string is technically iterable/non-empty in Python but is not a
    list of candidates -- must be rejected, not silently iterated char by
    char."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url, param_grid={"portfolio.n_long": "10"})
    validator = _mock_validator()
    pipeline = _make_pipeline(db_url)
    pipeline._wf_validator = validator

    with pytest.raises(MissingParameterGridError):
        pipeline.run(
            "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
        )

    validator.run.assert_not_called()


def test_valid_grid_still_runs(db_url: str) -> None:
    """Sanity check: a well-formed grid (every value a non-empty list)
    still passes the precondition and runs normally."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url, param_grid={"portfolio.n_long": [10, 20]})
    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust"),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.promotion_decision_id is not None


# ── FIX 2: the sweep must use the FROZEN grid, not a possibly-stale pre-freeze
#    read (closes the read/freeze TOCTOU) ──────────────────────────────────────


def test_sweep_uses_frozen_grid_not_stale_pre_freeze_read(db_url: str) -> None:
    """Simulates a concurrent HypothesisRegistry.update_param_grid()
    committing between PromotionPipeline.run()'s initial fail-closed
    precondition read (_resolve_frozen_grid, called before ANY instrument
    runs) and the freeze TrialRecorder.run_walk_forward applies as a side
    effect of its first trial. The sweep dispatched afterward must use the
    grid as it stood AT FREEZE TIME (the immutable frozen record), not the
    stale value read before the race landed."""
    config_hash = _seed_definition(db_url)
    grid_before_race = {"portfolio.n_long": [10, 20]}
    grid_after_race = {"portfolio.n_long": [5, 15, 25]}
    hyp_id = _seed_hypothesis(db_url, param_grid=grid_before_race)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust"),
    )

    real_get_hypothesis = pipeline._hypothesis_registry.get_hypothesis
    call_count = {"n": 0}

    def _get_hypothesis_with_simulated_race(hypothesis_id: int):
        hyp = real_get_hypothesis(hypothesis_id)
        call_count["n"] += 1
        if call_count["n"] == 1:
            # This is the FIRST read -- the initial fail-closed precondition
            # check at the top of run(), before run_walk_forward (and
            # therefore before the freeze) has happened. Land the "racing"
            # update directly against the row (bypassing the frozen_at-
            # guarded update_param_grid() API on purpose -- this models a
            # commit that landed a moment before TrialRecorder's atomic
            # freeze, not a call through the guarded write path) so that by
            # the time the freeze fires, param_grid_json is already
            # grid_after_race.
            engine = _raw_engine(db_url)
            with Session(engine) as session:
                row = session.get(StrategyHypothesis, hypothesis_id)
                row.param_grid_json = grid_after_race
                session.commit()
        return hyp

    with patch.object(
        pipeline._hypothesis_registry,
        "get_hypothesis",
        side_effect=_get_hypothesis_with_simulated_race,
    ):
        pipeline.run(
            "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
        )

    # get_hypothesis was called (at least) twice: once for the initial
    # precondition, once more to re-read the FROZEN value for the sweep.
    assert call_count["n"] >= 2

    sweeper = pipeline._sweeper
    sweeper.sweep.assert_called_once()
    dispatched_param_grid = sweeper.sweep.call_args.args[1]
    assert dispatched_param_grid == grid_after_race
    assert dispatched_param_grid != grid_before_race

    # The hypothesis is now frozen and its persisted grid is the one the
    # sweep used -- provably the immutable frozen record.
    frozen_hyp = pipeline._hypothesis_registry.get_hypothesis(hyp_id)
    assert frozen_hyp.frozen_at is not None
    assert frozen_hyp.param_grid_json == grid_after_race


# ── n_trials sweep-counting: a sweep counts as configs_tested, not 1 ───────────


def test_n_trials_counts_sweep_variants_not_one(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust", configs_tested=9),
    )
    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    # 1 walk_forward trial + 9 (NOT 1) for the sweep invocation.
    assert result.n_trials_used == 10
    assert result.n_trials_used != 2  # guards against the "1 per sweep call" bug


# ── Residual-bug flags (BUG-066/068/071) always present ────────────────────────


def test_residual_bug_flags_present_in_evidence(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)
    pipeline = _make_pipeline(db_url)

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    flags = result.evidence_json["residual_bug_acknowledgements"]
    assert set(flags.keys()) == {"BUG-066", "BUG-068", "BUG-071"}
    for bug_id in ("BUG-066", "BUG-068", "BUG-071"):
        assert flags[bug_id]["status"] == "open"
        assert flags[bug_id]["description"]
    assert flags == RESIDUAL_BUG_ACKNOWLEDGEMENTS

    # Also present on a failing run -- the caveat must survive regardless of verdict.
    fail_pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(sharpe=0.0),
        sweep_result=_sweep_result(verdict="curve_fit"),
    )
    fail_result = fail_pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )
    assert "BUG-066" in fail_result.evidence_json["residual_bug_acknowledgements"]


# ── FIX 3: holdout_mode=True is gated/deferred -- fails closed immediately ─────


def test_holdout_mode_fails_closed_before_any_instrument_or_db_work(db_url: str) -> None:
    """holdout_mode=True must raise HoldoutConfirmationNotSupportedError
    immediately, before ANY instrument (walk-forward, sweep) runs and
    before any DB work (no strategy_trials or promotion_decisions rows).

    This replaces the old test_holdout_mode_skips_sensitivity_sweep /
    test_promotion_result_dsr_none_when_n_trials_is_one_and_fdr_skips
    tests, which propped up holdout confirmation via a separate
    holdout-dated StrategyDefinition + a hand-seeded ResearchDataWindow --
    the exact flawed "confirms a DIFFERENT config than the frozen winner"
    workaround this fix gates closed. holdout_mode is deferred pending
    docs/plans/04-identity-evaluation-context-design.md; no test should
    exercise the old path as if it worked.
    """
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)
    validator = _mock_validator()
    sweeper = _mock_sweeper()
    pipeline = _make_pipeline(db_url)
    pipeline._wf_validator = validator
    pipeline._sweeper = sweeper

    with pytest.raises(HoldoutConfirmationNotSupportedError):
        pipeline.run(
            "v1_test_strategy",
            config_hash,
            data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW,
            hypothesis_id=hyp_id,
            holdout_mode=True,
        )

    validator.run.assert_not_called()
    sweeper.sweep.assert_not_called()

    trials = pipeline._trial_recorder.list_trials("v1_test_strategy")
    assert trials == []

    engine = _raw_engine(db_url)
    with Session(engine) as session:
        decisions = list(
            session.scalars(
                select(PromotionDecision).where(
                    PromotionDecision.strategy_id == "v1_test_strategy"
                )
            )
        )
    assert decisions == []


def test_holdout_mode_default_false_train_oos_path_unaffected(db_url: str) -> None:
    """The default holdout_mode=False (train/OOS) path -- the core of this
    slice -- must remain fully functional: no gating error, sensitivity
    sweep still runs, overall_passed still derived from all three gates."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)
    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust"),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.sensitivity_result is not None
    assert result.sensitivity_verdict == "robust"
    assert result.evidence_json["sensitivity"]["skipped"] is False
    assert result.overall_passed == (
        result.funnel_passed
        and result.stress_verdict == "solid"
        and result.sensitivity_verdict == "robust"
    )
    assert result.promotion_decision_id is not None


# ── MLflow additive DSR/FDR logging path ────────────────────────────────────────


def test_dsr_fdr_logged_via_mlflow_additive_path(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    mock_logger = MagicMock(spec=BacktestLogger)
    mock_logger.log_walk_forward_run.return_value = "fake-run-id-123"

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust"),
        backtest_logger=mock_logger,
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    mock_logger.log_walk_forward_run.assert_called_once()
    call_kwargs = mock_logger.log_walk_forward_run.call_args.kwargs
    assert call_kwargs["funnel_result"] is result.funnel_result
    assert call_kwargs["stress_result"] is result.stress_result
    # Round-6 (PR #50 Codex P2): the MLflow-internal "config_hash" tag is a
    # naive hash of the dispatched config (includes data_version/eval_window),
    # which diverges from the canonical identity hash and would change across
    # runs of the same strategy over different windows. A separately named
    # "canonical_config_hash" tag carries the SAME config_hash this promotion
    # decision is keyed by, so an MLflow run can be correlated back to its
    # promotion_decisions/strategy_definitions row.
    assert call_kwargs["tags"] == {"canonical_config_hash": config_hash}

    mock_logger.log_promotion_decision.assert_called_once()
    log_kwargs = mock_logger.log_promotion_decision.call_args.kwargs
    assert log_kwargs["run_id"] == "fake-run-id-123"
    assert log_kwargs["dsr_value"] == pytest.approx(result.dsr_value)
    assert log_kwargs["n_trials"] == result.n_trials_used
    assert log_kwargs["n_observations"] == len(_oos_returns().dropna())

    assert result.mlflow_run_id == "fake-run-id-123"


def test_pipeline_works_without_mlflow_logger(db_url: str) -> None:
    """backtest_logger=None (the default) must not raise -- pipeline still
    completes and persists a promotion_decisions row with mlflow_run_id=None."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)
    pipeline = _make_pipeline(db_url, backtest_logger=None)

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.mlflow_run_id is None
    assert result.promotion_decision_id is not None


# ── FIX 1: the DISPATCHED config (wf_result.config), not the original ───────
# StrategyDefinition.config, must be passed to log_walk_forward_run.


def test_mlflow_logging_passes_dispatched_config_not_original(db_url: str) -> None:
    """TrialRecorder dispatches a COPY of the strategy config with
    data_version injected (04-3), and WalkForwardValidator returns that copy
    as wf_result.config. _log_to_mlflow must forward wf_result.config to
    log_walk_forward_run, not the original StrategyDefinition.config --
    otherwise log_walk_forward_run's real provenance check (hash(config) ==
    hash(wf_result.config)) always fails and MLflow logging never succeeds
    in production."""
    original_config = _config("v1_test_strategy")
    config_hash = _seed_definition(db_url, config=original_config)
    hyp_id = _seed_hypothesis(db_url)

    # The dispatched copy differs from the original (e.g. data_version
    # injected by TrialRecorder), so its hash differs too.
    dispatched_config = dict(original_config, data_version=DATA_VERSION)
    base_wf = _wf_result()
    dispatched_wf = WalkForwardResult(
        folds=base_wf.folds,
        oos_returns=base_wf.oos_returns,
        oos_metrics=base_wf.oos_metrics,
        config=dispatched_config,
    )

    # Simulate mlflow_logger.log_walk_forward_run's real provenance check
    # (hash equality between the passed `config` arg and `wf_result.config`)
    # using the actual hashing/exception types, so this test would fail if
    # the pipeline reverted to passing the original config.
    from backtesting.config_contract import ConfigProvenanceMismatchError
    from backtesting.experiment_tracking.mlflow_logger import _hash_config

    def _fake_log_walk_forward_run(config, wf_result_arg, experiment_name, **kwargs):
        if _hash_config(config) != _hash_config(wf_result_arg.config):
            raise ConfigProvenanceMismatchError(
                f"passed config hash {_hash_config(config)} != "
                f"wf_result.config hash {_hash_config(wf_result_arg.config)}"
            )
        return "fake-run-id-789"

    # Prove the ORIGINAL-config path would have failed this check.
    with pytest.raises(ConfigProvenanceMismatchError):
        _fake_log_walk_forward_run(original_config, dispatched_wf, "exp")

    mock_logger = MagicMock(spec=BacktestLogger)
    mock_logger.log_walk_forward_run.side_effect = _fake_log_walk_forward_run

    pipeline = _make_pipeline(
        db_url,
        validator_result=dispatched_wf,
        sweep_result=_sweep_result(verdict="robust"),
        backtest_logger=mock_logger,
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    mock_logger.log_walk_forward_run.assert_called_once()
    call = mock_logger.log_walk_forward_run.call_args
    passed_config = call.args[0] if call.args else call.kwargs["config"]
    assert passed_config == dispatched_config
    assert passed_config != original_config

    # MLflow logging SUCCEEDS on the happy path (not silently None).
    assert result.mlflow_run_id == "fake-run-id-789"
    mock_logger.log_promotion_decision.assert_called_once()
    log_kwargs = mock_logger.log_promotion_decision.call_args.kwargs
    assert log_kwargs["run_id"] == "fake-run-id-789"


# ── FIX 1: a supplied-but-failing MLflow logger must degrade gracefully ─────
# (must NOT discard an otherwise-complete promotion_decisions row).


@pytest.mark.parametrize(
    "failing_method", ["log_walk_forward_run", "log_promotion_decision"]
)
def test_failing_mlflow_logger_does_not_discard_promotion_decision(
    db_url: str, failing_method: str
) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    mock_logger = MagicMock(spec=BacktestLogger)
    mock_logger.log_walk_forward_run.return_value = "fake-run-id-456"
    getattr(mock_logger, failing_method).side_effect = RuntimeError("mlflow outage")

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust", configs_tested=6),
        backtest_logger=mock_logger,
    )

    with structlog.testing.capture_logs() as captured_logs:
        result = pipeline.run(
            "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
        )

    # The pipeline must still complete and return a real decision id.
    assert result.promotion_decision_id is not None
    assert result.mlflow_run_id is None
    assert result.overall_passed is True
    assert result.funnel_passed is True

    events = [entry.get("event") for entry in captured_logs]
    assert "promotion_mlflow_logging_failed" in events
    failure_log = next(
        entry for entry in captured_logs if entry.get("event") == "promotion_mlflow_logging_failed"
    )
    assert failure_log["strategy_id"] == "v1_test_strategy"
    assert "mlflow outage" in failure_log["error"]

    # promotion_decisions row must be persisted with the real stage results
    # and mlflow_run_id NULL.
    engine = _raw_engine(db_url)
    with Session(engine) as session:
        row = session.get(PromotionDecision, result.promotion_decision_id)
        assert row is not None
        assert row.overall_passed is True
        assert row.funnel_passed is True
        assert row.mlflow_run_id is None
        assert row.n_trials_used == result.n_trials_used


def test_failing_db_persist_still_propagates(db_url: str) -> None:
    """The MLflow graceful-degradation try/except must NOT be so broad that
    it also swallows a failure in _persist_decision -- the DB write is the
    authoritative record, so a failure there must still raise."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust"),
    )

    with patch.object(
        type(pipeline), "_persist_decision", side_effect=RuntimeError("db write failed")
    ):
        with pytest.raises(RuntimeError, match="db write failed"):
            pipeline.run(
                "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
            )


# ── FIX 2: an undercounted configs_tested fallback must be observable ───────


def test_compute_n_trials_logs_warning_on_configs_tested_fallback(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)
    pipeline = _make_pipeline(db_url)

    engine = _raw_engine(db_url)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            StrategyTrial(
                strategy_id="v1_test_strategy",
                config_hash=config_hash,
                hypothesis_id=hyp_id,
                window="train_oos",
                run_type="parameter_sweep_variant",
                data_version=DATA_VERSION,
                status="completed",
                metrics_json={},  # missing configs_tested -- corrupted/legacy row
                started_at=now,
                completed_at=now,
            )
        )
        session.commit()

    with structlog.testing.capture_logs() as captured_logs:
        n_trials = pipeline._compute_n_trials("v1_test_strategy")

    assert n_trials == 1  # fallback still counts 1

    events = [entry.get("event") for entry in captured_logs]
    assert "promotion_n_trials_configs_tested_fallback" in events
    fallback_log = next(
        entry
        for entry in captured_logs
        if entry.get("event") == "promotion_n_trials_configs_tested_fallback"
    )
    assert fallback_log["strategy_id"] == "v1_test_strategy"
    assert fallback_log["raw_value"] == repr(None)


def test_compute_n_trials_no_warning_on_well_formed_sweep_row(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)
    pipeline = _make_pipeline(db_url)

    engine = _raw_engine(db_url)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            StrategyTrial(
                strategy_id="v1_test_strategy",
                config_hash=config_hash,
                hypothesis_id=hyp_id,
                window="train_oos",
                run_type="parameter_sweep_variant",
                data_version=DATA_VERSION,
                status="completed",
                metrics_json={"configs_tested": 5},
                started_at=now,
                completed_at=now,
            )
        )
        session.commit()

    with structlog.testing.capture_logs() as captured_logs:
        n_trials = pipeline._compute_n_trials("v1_test_strategy")

    assert n_trials == 5

    events = [entry.get("event") for entry in captured_logs]
    assert "promotion_n_trials_configs_tested_fallback" not in events


def test_compute_n_trials_counts_planned_size_on_errored_sweep_row(db_url: str) -> None:
    """A crashed/errored parameter_sweep_variant row that still carries the
    PLANNED configs_tested (seeded by TrialRecorder.run_parameter_sweep at
    insert time, before dispatch) must contribute its planned count to
    n_trials, not silently fall back to 1 -- an undercount here inflates the
    deflated Sharpe by understating the multiple-testing penalty."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)
    pipeline = _make_pipeline(db_url)

    engine = _raw_engine(db_url)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            StrategyTrial(
                strategy_id="v1_test_strategy",
                config_hash=config_hash,
                hypothesis_id=hyp_id,
                window="train_oos",
                run_type="parameter_sweep_variant",
                data_version=DATA_VERSION,
                status="errored",
                metrics_json={
                    "configs_tested": 6,  # planned 2x3 grid, seeded pre-dispatch
                    "error_type": "RuntimeError",
                    "error_message": "boom",
                },
                started_at=now,
                completed_at=now,
            )
        )
        session.commit()

    with structlog.testing.capture_logs() as captured_logs:
        n_trials = pipeline._compute_n_trials("v1_test_strategy")

    assert n_trials == 6

    events = [entry.get("event") for entry in captured_logs]
    assert "promotion_n_trials_configs_tested_fallback" not in events


# ── FIX 1: non-finite metrics must not block persistence ────────────────────


def _assert_no_nan(obj: object) -> None:
    """Recursively assert no non-finite float survives anywhere in obj --
    used to confirm evidence_json is JSONB-safe after sanitization."""
    if isinstance(obj, dict):
        for v in obj.values():
            _assert_no_nan(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _assert_no_nan(v)
    elif isinstance(obj, float):
        assert math.isfinite(obj), f"non-finite float leaked into evidence_json: {obj!r}"


def test_sanitize_metrics_recursive_normalizer_on_nested_structure() -> None:
    """Direct unit test of the reused TrialRecorder normalizer on a nested
    dict/list containing NaN/inf -- promotion_pipeline imports this exact
    function rather than reimplementing it."""
    nested = {
        "a": float("nan"),
        "b": [1.0, float("inf"), {"c": float("-inf"), "d": 2.5}],
        "e": (float("nan"), 3),
        "f": None,
        "g": "unchanged",
        "h": np.float64("nan"),
        "i": np.int64(7),
    }

    sanitized = _sanitize_metrics(nested)

    assert sanitized["a"] is None
    assert sanitized["b"][0] == 1.0
    assert sanitized["b"][1] is None
    assert sanitized["b"][2] == {"c": None, "d": 2.5}
    assert sanitized["e"] == [None, 3]
    assert sanitized["f"] is None
    assert sanitized["g"] == "unchanged"
    assert sanitized["h"] is None
    assert sanitized["i"] == 7
    _assert_no_nan(sanitized)


def test_all_nan_funnel_and_sensitivity_still_persists_decision(db_url: str) -> None:
    """Force NaN survival-funnel gate values (missing/non-finite OOS metrics)
    and an all-NaN sensitivity sweep, then confirm _persist_decision still
    SUCCEEDS -- the graceful-degradation guarantee this pipeline exists to
    provide. Before FIX 1, this raised (Postgres JSONB rejects NaN; the
    04-1 dsr_value NUMERIC CHECK rejects NaN) and NO audit row was written,
    exactly on a FAILED promotion where the audit trail matters most."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    # oos sharpe/max_drawdown missing entirely -> SurvivalFunnel._f(None)
    # yields NaN gate values; zero trades -> deterministic (non-NaN) fail.
    wf = _wf_result(
        sharpe=float("nan"), max_dd=float("nan"), is_sharpe=float("nan"), trade_count=0
    )

    sweep = ParameterSensitivityResult(
        base_config_name="v1_test_strategy",
        param_grid={"portfolio.n_long": [10, 20]},
        configs_tested=2,
        rows=[],
        mean_oos_sharpe=float("nan"),
        std_oos_sharpe=float("nan"),
        positive_fraction=float("nan"),
        curve_fit_flag=True,
        verdict="curve_fit",
    )

    pipeline = _make_pipeline(db_url, validator_result=wf, sweep_result=sweep)

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    assert result.overall_passed is False
    assert result.promotion_decision_id is not None

    engine = _raw_engine(db_url)
    with Session(engine) as session:
        row = session.get(PromotionDecision, result.promotion_decision_id)
        assert row is not None
        assert row.overall_passed is False
        # Non-finite dsr_value (or the None _compute_dsr already returns for
        # a non-finite observed_sharpe) must persist as None, not NaN.
        assert row.dsr_value is None
        _assert_no_nan(row.evidence_json)
        gate_values = [g["value"] for g in row.evidence_json["funnel"]["gates"]]
        assert any(v is None for v in gate_values)
        # "mean_oos_sharpe"/"std_oos_sharpe"/"positive_fraction" are now the
        # round-6 AUTHORITATIVE (deduped) stats, computed by summarize_variants
        # over zero finite rows -- matching real ParameterSweeper.sweep
        # semantics for n_valid=0 (mean=NaN->None, but std/positive_fraction
        # are legitimately 0.0, not NaN). The "raw_*" counterparts are still
        # this fixture's directly hand-set NaN values, sanitized to None --
        # that's what actually exercises the NaN-sanitization path this test
        # is about.
        assert row.evidence_json["sensitivity"]["mean_oos_sharpe"] is None
        assert row.evidence_json["sensitivity"]["std_oos_sharpe"] == 0.0
        assert row.evidence_json["sensitivity"]["positive_fraction"] == 0.0
        assert row.evidence_json["sensitivity"]["raw_mean_oos_sharpe"] is None
        assert row.evidence_json["sensitivity"]["raw_std_oos_sharpe"] is None
        assert row.evidence_json["sensitivity"]["raw_positive_fraction"] is None


def test_nan_dsr_from_deflated_sharpe_fn_normalized_to_none(db_url: str) -> None:
    """Even when observed_sharpe/n_trials/n_observations are all finite and
    sufficient (so _compute_dsr actually calls the injected
    deflated_sharpe_fn), a NaN returned directly from that fn must be
    normalized to None at the SOURCE (_compute_dsr), not merely at the DB
    persistence boundary -- so every downstream consumer (the returned
    PromotionResult, the FDR evidence, evidence_json, and the DB row) agrees
    on the same normalized value."""
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url)

    pipeline = _make_pipeline(
        db_url,
        validator_result=_wf_result(),
        sweep_result=_sweep_result(verdict="robust"),
        deflated_sharpe_fn=lambda **kwargs: float("nan"),
    )

    result = pipeline.run(
        "v1_test_strategy", config_hash, data_handler=MagicMock(), eval_window=DEFAULT_EVAL_WINDOW, hypothesis_id=hyp_id
    )

    # Source-normalized: None everywhere, never a raw NaN surfacing anywhere.
    assert result.dsr_value is None

    # FDR evidence must treat this run's own DSR as unavailable (skipped),
    # not clamp a NaN into p=1.0.
    assert result.evidence_json["overfitting"]["fdr"]["skipped"] is True
    assert result.evidence_json["overfitting"]["dsr_value"] is None

    engine = _raw_engine(db_url)
    with Session(engine) as session:
        row = session.get(PromotionDecision, result.promotion_decision_id)
        assert row is not None
        assert row.dsr_value is None
        assert row.evidence_json["overfitting"]["dsr_value"] is None


# ── FIX 2: FDR must use each sibling's LATEST decision, or omit it ──────────


def _seed_sibling_strategy(db_url: str, strategy_id: str, family: str) -> str:
    """Seed a strategy_definitions + strategies row (direct session.add,
    bypassing StrategyRegistry.register()'s YAML-file requirement) so
    _compute_family_fdr's sibling lookup by strategy_family finds it."""
    config_hash = _seed_definition(db_url, strategy_id=strategy_id)
    engine = _raw_engine(db_url)
    with Session(engine) as session:
        session.add(
            Strategy(
                strategy_id=strategy_id,
                canonical_config_hash=config_hash,
                status="backtesting",
                strategy_family=family,
                registered_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    return config_hash


def _seed_promotion_decision(
    db_url: str,
    *,
    strategy_id: str,
    config_hash: str,
    dsr_value: float | None,
    created_at: datetime,
) -> None:
    engine = _raw_engine(db_url)
    with Session(engine) as session:
        session.add(
            PromotionDecision(
                strategy_id=strategy_id,
                config_hash=config_hash,
                n_trials_used=1,
                dsr_value=dsr_value,
                funnel_passed=True,
                sensitivity_verdict="robust",
                stress_verdict="solid",
                overall_passed=True,
                mlflow_run_id=None,
                evidence_json={},
                created_at=created_at,
            )
        )
        session.commit()


def test_fdr_omits_sibling_whose_newest_decision_has_no_dsr_value(db_url: str) -> None:
    """Sibling with TWO promotion_decisions rows: newest dsr_value=None,
    older finite. FIX 2 requires the sibling be OMITTED from the FDR set
    entirely (newest-or-omit), never falling back to the stale older row."""
    config_hash = _seed_definition(db_url)
    pipeline = _make_pipeline(db_url)

    sibling_id = "v1_sibling_strategy"
    sibling_hash = _seed_sibling_strategy(db_url, sibling_id, family="fam1")

    now = datetime.now(timezone.utc)
    _seed_promotion_decision(
        db_url,
        strategy_id=sibling_id,
        config_hash=sibling_hash,
        dsr_value=0.75,  # older, finite -- must NOT be used
        created_at=now - pd.Timedelta(hours=2),
    )
    _seed_promotion_decision(
        db_url,
        strategy_id=sibling_id,
        config_hash=sibling_hash,
        dsr_value=None,  # newest -- unusable, sibling must be omitted
        created_at=now - pd.Timedelta(hours=1),
    )

    fdr_evidence = pipeline._compute_family_fdr("v1_test_strategy", "fam1", dsr_value=0.6)

    assert fdr_evidence["skipped"] is False
    assert sibling_id not in fdr_evidence["compared_strategy_ids"]
    assert fdr_evidence["compared_strategy_ids"] == ["v1_test_strategy"]


def test_fdr_includes_sibling_whose_newest_decision_has_finite_dsr_value(db_url: str) -> None:
    """A sibling whose NEWEST row has a finite dsr_value IS included, even
    when an older row also exists."""
    config_hash = _seed_definition(db_url)
    pipeline = _make_pipeline(db_url)

    sibling_id = "v1_sibling_strategy"
    sibling_hash = _seed_sibling_strategy(db_url, sibling_id, family="fam1")

    now = datetime.now(timezone.utc)
    _seed_promotion_decision(
        db_url,
        strategy_id=sibling_id,
        config_hash=sibling_hash,
        dsr_value=None,  # older, unusable
        created_at=now - pd.Timedelta(hours=2),
    )
    _seed_promotion_decision(
        db_url,
        strategy_id=sibling_id,
        config_hash=sibling_hash,
        dsr_value=0.8,  # newest, finite -- must be used
        created_at=now - pd.Timedelta(hours=1),
    )

    fdr_evidence = pipeline._compute_family_fdr("v1_test_strategy", "fam1", dsr_value=0.6)

    assert fdr_evidence["skipped"] is False
    assert sibling_id in fdr_evidence["compared_strategy_ids"]
    idx = fdr_evidence["compared_strategy_ids"].index(sibling_id)
    assert fdr_evidence["p_values"][idx] == pytest.approx(1.0 - 0.8)
