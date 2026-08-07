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

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from backtesting.experiment_tracking.mlflow_logger import BacktestLogger
from backtesting.validation.bootstrap_stress import BootstrapStressResult
from backtesting.validation.parameter_sensitivity import (
    ParameterSensitivityResult,
    ParameterSweeper,
)
from backtesting.validation.promotion_pipeline import (
    MissingParameterGridError,
    PromotionPipeline,
    RESIDUAL_BUG_ACKNOWLEDGEMENTS,
)
from backtesting.validation.walk_forward import WalkForwardFold, WalkForwardResult, WalkForwardValidator
from strategy_registry.fingerprint import hash_config
from strategy_registry.hypothesis import HypothesisRegistry
from strategy_registry.models import Base, StrategyDefinition
from strategy_registry.selection_models import PromotionDecision, StrategyTrial

DATA_VERSION = "a" * 64


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


def _sweep_result(verdict: str = "robust", configs_tested: int = 6) -> ParameterSensitivityResult:
    return ParameterSensitivityResult(
        base_config_name="v1_test_strategy",
        param_grid={"portfolio.n_long": [10, 20]},
        configs_tested=configs_tested,
        rows=[],
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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
    )

    assert result.funnel_passed is True
    assert result.sensitivity_verdict == "curve_fit"
    assert result.overall_passed is False
    assert result.evidence_json["sensitivity"]["verdict"] == "curve_fit"


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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
    )

    assert result.dsr_value is not None
    assert 0.0 <= result.dsr_value <= 1.0


# ── Fail closed on missing/unlinked/empty hypothesis grid ──────────────────────


def test_missing_hypothesis_id_fails_closed(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    validator = _mock_validator()
    pipeline = _make_pipeline(db_url)
    # Swap in a spy validator so we can prove it was never dispatched.
    pipeline._wf_validator = validator

    with pytest.raises(MissingParameterGridError):
        pipeline.run("v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=None)

    validator.run.assert_not_called()


def test_hypothesis_with_no_param_grid_fails_closed(db_url: str) -> None:
    config_hash = _seed_definition(db_url)
    hyp_id = _seed_hypothesis(db_url, param_grid=None)
    validator = _mock_validator()
    pipeline = _make_pipeline(db_url)
    pipeline._wf_validator = validator

    with pytest.raises(MissingParameterGridError):
        pipeline.run(
            "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
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
            data_handler=MagicMock(),
            hypothesis_id=other_hyp_id,
        )


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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
    )
    assert "BUG-066" in fail_result.evidence_json["residual_bug_acknowledgements"]


# ── holdout_mode: skips sensitivity, gates on funnel+stress only ──────────────


def test_holdout_mode_skips_sensitivity_sweep(db_url: str) -> None:
    holdout_config = {
        "name": "v1_test_strategy",
        "version": 1,
        "backtest": {"start_date": "2023-02-01", "end_date": "2023-06-01"},
    }
    config_hash = _seed_definition(db_url, config=holdout_config)
    hyp_id = _seed_hypothesis(db_url)
    engine = _raw_engine(db_url)
    from strategy_registry.selection_models import ResearchDataWindow

    with Session(engine) as session:
        session.add(
            ResearchDataWindow(
                strategy_id="v1_test_strategy",
                train_start=date(2022, 1, 1),
                train_end=date(2022, 7, 1),
                oos_start=date(2022, 7, 1),
                oos_end=date(2023, 1, 1),
                holdout_start=date(2023, 1, 1),
                holdout_end=date(2023, 7, 1),
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    holdout_wf = _wf_result()
    holdout_wf.oos_returns.index = pd.date_range("2023-01-02", periods=len(holdout_wf.oos_returns), freq="D")
    pipeline = _make_pipeline(db_url, validator_result=holdout_wf)

    result = pipeline.run(
        "v1_test_strategy",
        config_hash,
        data_handler=MagicMock(),
        hypothesis_id=hyp_id,
        holdout_mode=True,
    )

    assert result.sensitivity_result is None
    assert result.sensitivity_verdict is None
    assert result.evidence_json["sensitivity"]["skipped"] is True
    # overall_passed derived from funnel + stress only.
    assert result.overall_passed == (result.funnel_passed and result.stress_verdict == "solid")

    trials = pipeline._trial_recorder.list_trials("v1_test_strategy")
    assert all(t.run_type != "parameter_sweep_variant" for t in trials)
    assert any(t.run_type == "holdout_confirmation" for t in trials)


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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
    )

    mock_logger.log_walk_forward_run.assert_called_once()
    call_kwargs = mock_logger.log_walk_forward_run.call_args.kwargs
    assert call_kwargs["funnel_result"] is result.funnel_result
    assert call_kwargs["stress_result"] is result.stress_result

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
        "v1_test_strategy", config_hash, data_handler=MagicMock(), hypothesis_id=hyp_id
    )

    assert result.mlflow_run_id is None
    assert result.promotion_decision_id is not None
