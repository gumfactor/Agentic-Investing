"""Tests for TrialRecorder (Gate 04 slice 04-2,
docs/plans/04-strategy-selection-protocol-design.md §4.1, §4.2, §7 row 04-2).

Mirrors the DB-testing convention established in
tests/strategy_registry/test_selection_models.py: SQLite with
``PRAGMA foreign_keys=ON`` so the 04-1 composite FKs and the one-shot-seal
partial unique index are genuinely enforced, not merely schema-shape checks.

Per the task brief, no real backtests run here and no broker/DB besides the
throwaway SQLite file is touched: the wrapped ``WalkForwardValidator``/
``ParameterSweeper`` instruments are ``unittest.mock`` doubles configured to
return canned results or raise, so these tests exercise TrialRecorder's own
recording/guard logic in isolation from the real engine.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import structlog.testing

from backtesting.config_contract import ConfigProvenanceMismatchError
from backtesting.validation.parameter_sensitivity import (
    ParameterSensitivityResult,
    ParameterSweeper,
)
from backtesting.validation.trial_recorder import (
    DataVersionProvenanceMismatchError,
    HoldoutWindowViolationError,
    SweepWindowOverrideError,
    TrialRecorder,
)
from backtesting.validation.walk_forward import WalkForwardResult, WalkForwardValidator
from strategy_registry.fingerprint import hash_config
from strategy_registry.registry import DefinitionNotFoundError, MissingDataVersionError
from strategy_registry.models import StrategyDefinition
from strategy_registry.selection_models import ResearchDataWindow, StrategyHypothesis
from sqlalchemy.orm import Session

DATA_VERSION = "a" * 64


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'trial_recorder.db'}"


@pytest.fixture
def recorder(db_url: str) -> TrialRecorder:
    return TrialRecorder(db_url)


def _seed_definition(
    recorder: TrialRecorder,
    *,
    strategy_id: str = "v1_test_strategy",
    config: dict | None = None,
    config_hash: str | None = None,
) -> str:
    """Seed a ``strategy_definitions`` row and return the ``config_hash`` used.

    FIX 1 (config-provenance check): most tests now need the seeded
    ``config_hash`` to be the REAL canonical hash of whatever config dict
    they later pass to ``run_walk_forward``/``run_parameter_sweep``, or the
    new provenance check rejects them. Pass ``config=`` (the exact dict the
    test will dispatch with) to get a hash that will genuinely match; tests
    that only exercise a guard which rejects BEFORE the provenance check
    (e.g. the holdout-overlap tests) can omit it and keep the arbitrary
    legacy placeholder hash, since it is never compared in that path.
    """
    if config_hash is None:
        config_hash = hash_config(config) if config is not None else "a" * 64
    with Session(recorder._engine) as session:
        session.add(
            StrategyDefinition(
                strategy_id=strategy_id,
                config_hash=config_hash,
                name=strategy_id,
                version=1,
                config={"foo": "bar"},
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    return config_hash


def _seed_hypothesis(
    recorder: TrialRecorder,
    *,
    strategy_id: str = "v1_test_strategy",
    hypothesis_text: str = "Momentum window sensitivity",
    param_grid_json: dict | None = None,
) -> int:
    """Seed a strategy_hypotheses row with frozen_at NULL and return its id."""
    with Session(recorder._engine) as session:
        hyp = StrategyHypothesis(
            strategy_id=strategy_id,
            hypothesis_text=hypothesis_text,
            param_grid_json=param_grid_json or {"momentum_window": [3, 6, 12]},
            created_at=datetime.now(timezone.utc),
        )
        session.add(hyp)
        session.commit()
        session.refresh(hyp)
        return hyp.id


def _seed_window(
    recorder: TrialRecorder,
    *,
    strategy_id: str = "v1_test_strategy",
    train_start: date = date(2022, 1, 1),
    train_end: date = date(2022, 7, 1),
    oos_start: date = date(2022, 7, 1),
    oos_end: date = date(2023, 1, 1),
    holdout_start: date = date(2023, 1, 1),
    holdout_end: date = date(2023, 7, 1),
) -> None:
    with Session(recorder._engine) as session:
        session.add(
            ResearchDataWindow(
                strategy_id=strategy_id,
                train_start=train_start,
                train_end=train_end,
                oos_start=oos_start,
                oos_end=oos_end,
                holdout_start=holdout_start,
                holdout_end=holdout_end,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()


def _config(start: str, end: str) -> dict:
    return {
        "name": "v1_test_strategy",
        "backtest": {"start_date": start, "end_date": end},
    }


def _wf_result(sharpe: float = 1.2, max_dd: float = -0.1) -> WalkForwardResult:
    return WalkForwardResult(
        folds=[],
        oos_returns=pd.Series(dtype=float),
        oos_metrics={"sharpe": sharpe, "max_drawdown": max_dd, "cagr": 0.15},
        config={},
    )


def _sweep_result(
    mean_sharpe: float = 0.8,
    verdict: str = "robust",
) -> ParameterSensitivityResult:
    return ParameterSensitivityResult(
        base_config_name="v1_test_strategy",
        param_grid={"portfolio.n_long": [10, 20]},
        configs_tested=2,
        rows=[],
        mean_oos_sharpe=mean_sharpe,
        std_oos_sharpe=0.1,
        positive_fraction=1.0,
        curve_fit_flag=False,
        verdict=verdict,
    )


def _mock_validator(result=None, exc=None) -> MagicMock:
    mock = MagicMock(spec=WalkForwardValidator)
    if exc is not None:
        mock.run.side_effect = exc
    else:
        mock.run.return_value = result if result is not None else _wf_result()
    return mock


def _mock_sweeper(result=None, exc=None) -> MagicMock:
    mock = MagicMock(spec=ParameterSweeper)
    if exc is not None:
        mock.sweep.side_effect = exc
    else:
        mock.sweep.return_value = result if result is not None else _sweep_result()
    return mock


# ── Successful walk-forward run inside train/OOS ────────────────────────────────


def test_walk_forward_inside_train_oos_succeeds_and_records_one_row(recorder: TrialRecorder) -> None:
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result(sharpe=1.5, max_dd=-0.12))

    result = recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
    )

    assert result.oos_metrics["sharpe"] == 1.5
    validator.run.assert_called_once()

    trials = recorder.list_trials("v1_test_strategy")
    assert len(trials) == 1
    trial = trials[0]
    assert trial.window == "train_oos"
    assert trial.run_type == "walk_forward"
    assert trial.status == "completed"
    assert float(trial.oos_sharpe) == 1.5
    assert float(trial.oos_max_drawdown) == -0.12
    assert trial.metrics_json["sharpe"] == 1.5
    assert trial.completed_at is not None


# ── Holdout-overlap rejection without dispatch ──────────────────────────────────


def test_run_overlapping_holdout_window_is_rejected_without_dispatch(recorder: TrialRecorder) -> None:
    _seed_definition(recorder)
    _seed_window(recorder)  # holdout = 2023-01-01 .. 2023-07-01
    validator = _mock_validator()

    # Overlaps the holdout window (ends inside it).
    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            config=_config("2022-06-01", "2023-03-01"),
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_run_fully_inside_holdout_window_is_rejected_without_dispatch(recorder: TrialRecorder) -> None:
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator()

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            config=_config("2023-02-01", "2023-04-01"),
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()


def test_parameter_sweep_overlapping_holdout_window_is_rejected_without_dispatch(
    recorder: TrialRecorder,
) -> None:
    _seed_definition(recorder)
    _seed_window(recorder)
    sweeper = _mock_sweeper()

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            base_config=_config("2022-06-01", "2023-03-01"),
            param_grid={"portfolio.n_long": [10, 20]},
            data_handler=MagicMock(),
        )

    sweeper.sweep.assert_not_called()


# ── Errored run still leaves a row (Gap-1 closure) ──────────────────────────────


def test_run_that_raises_leaves_an_errored_row(recorder: TrialRecorder) -> None:
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(exc=RuntimeError("engine blew up mid-fold"))

    with pytest.raises(RuntimeError, match="engine blew up mid-fold"):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
        )

    trials = recorder.list_trials("v1_test_strategy")
    assert len(trials) == 1
    trial = trials[0]
    assert trial.status == "errored"
    assert trial.window == "train_oos"
    assert trial.run_type == "walk_forward"
    assert trial.oos_sharpe is None
    assert trial.metrics_json["error_type"] == "RuntimeError"
    assert "engine blew up mid-fold" in trial.metrics_json["error_message"]
    assert trial.completed_at is not None


def test_parameter_sweep_that_raises_leaves_an_errored_row(recorder: TrialRecorder) -> None:
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper(exc=ValueError("param_grid must not be empty."))

    with pytest.raises(ValueError):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=base_config,
            param_grid={"portfolio.n_long": [10, 20]},
            data_handler=MagicMock(),
        )

    trials = recorder.list_trials("v1_test_strategy", run_type="parameter_sweep_variant")
    assert len(trials) == 1
    assert trials[0].status == "errored"


def test_parameter_sweep_that_raises_still_records_planned_configs_tested(
    recorder: TrialRecorder,
) -> None:
    """FIX 2: the PLANNED variant count (Cartesian product of param_grid) is
    seeded into metrics_json['configs_tested'] at the INITIAL insert, before
    dispatch -- so a sweep that crashes partway through still leaves an
    'errored' row carrying its attempted count, not an empty metrics_json
    that PromotionPipeline._compute_n_trials would understate as 1."""
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper(exc=RuntimeError("sweep crashed mid-grid"))

    # A 2x3 grid -> planned Cartesian product of 6 variants.
    param_grid = {"portfolio.n_long": [10, 20], "signals.momentum_window": [3, 6, 12]}

    with pytest.raises(RuntimeError, match="sweep crashed mid-grid"):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=base_config,
            param_grid=param_grid,
            data_handler=MagicMock(),
        )

    trials = recorder.list_trials("v1_test_strategy", run_type="parameter_sweep_variant")
    assert len(trials) == 1
    trial = trials[0]
    assert trial.status == "errored"
    assert trial.metrics_json["configs_tested"] == 6
    assert trial.metrics_json["error_type"] == "RuntimeError"
    assert "sweep crashed mid-grid" in trial.metrics_json["error_message"]


def test_parameter_sweep_success_still_records_actual_configs_tested(
    recorder: TrialRecorder,
) -> None:
    """Happy path unchanged: on a SUCCESSFUL sweep, the recorded row's
    metrics_json['configs_tested'] reflects the actual
    ParameterSensitivityResult.configs_tested (which equals the planned
    product for a full grid), not merely the pre-dispatch planned seed."""
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    param_grid = {"portfolio.n_long": [10, 20], "signals.momentum_window": [3, 6, 12]}
    sweeper = _mock_sweeper(result=_sweep_result())
    sweeper.sweep.return_value = ParameterSensitivityResult(
        base_config_name="v1_test_strategy",
        param_grid=param_grid,
        configs_tested=6,
        rows=[],
        mean_oos_sharpe=0.8,
        std_oos_sharpe=0.1,
        positive_fraction=1.0,
        curve_fit_flag=False,
        verdict="robust",
    )

    recorder.run_parameter_sweep(
        sweeper,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        base_config=base_config,
        param_grid=param_grid,
        data_handler=MagicMock(),
    )

    trials = recorder.list_trials("v1_test_strategy", run_type="parameter_sweep_variant")
    assert len(trials) == 1
    trial = trials[0]
    assert trial.status == "completed"
    assert trial.metrics_json["configs_tested"] == 6


def test_parameter_sweep_malformed_param_grid_does_not_crash_seeding(
    recorder: TrialRecorder,
) -> None:
    """A malformed param_grid value (not a list-like of candidates) must not
    crash the planned-count seeding step -- it should leave configs_tested
    unset on the initial row rather than raising, deferring any validation
    error to the wrapped ParameterSweeper.sweep call itself."""
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper(exc=ValueError("bad grid"))

    with pytest.raises(ValueError, match="bad grid"):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=base_config,
            param_grid={"portfolio.n_long": 10},  # not a list -- malformed
            data_handler=MagicMock(),
        )

    trials = recorder.list_trials("v1_test_strategy", run_type="parameter_sweep_variant")
    assert len(trials) == 1
    trial = trials[0]
    assert trial.status == "errored"
    assert "configs_tested" not in trial.metrics_json


# ── Holdout confirmation: one-shot seal ─────────────────────────────────────────


def test_first_holdout_confirmation_succeeds_and_records_holdout_row(recorder: TrialRecorder) -> None:
    config = _config("2023-01-01", "2023-07-01")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)  # holdout = 2023-01-01 .. 2023-07-01
    validator = _mock_validator(_wf_result(sharpe=0.9, max_dd=-0.2))

    result = recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
        final_holdout_confirmation=True,
    )

    assert result.oos_metrics["sharpe"] == 0.9
    validator.run.assert_called_once()

    trials = recorder.list_trials("v1_test_strategy", run_type="holdout_confirmation")
    assert len(trials) == 1
    trial = trials[0]
    assert trial.window == "holdout"
    assert trial.run_type == "holdout_confirmation"
    assert trial.status == "completed"


def test_second_holdout_confirmation_is_rejected_by_app_layer_guard(recorder: TrialRecorder) -> None:
    """The app-layer guard must reject the second attempt itself -- not rely
    on catching the DB's IntegrityError from the partial unique index. Proven
    by asserting the raised type is HoldoutWindowViolationError (not
    IntegrityError) and that the wrapped instrument is never dispatched a
    second time.
    """
    config = _config("2023-01-01", "2023-07-01")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
        final_holdout_confirmation=True,
    )
    assert validator.run.call_count == 1

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
            final_holdout_confirmation=True,
        )

    # The wrapped instrument was NOT dispatched a second time -- the guard
    # tripped before the second call.
    assert validator.run.call_count == 1

    trials = recorder.list_trials("v1_test_strategy", run_type="holdout_confirmation")
    assert len(trials) == 1  # still exactly one -- second attempt never inserted a row


def test_second_holdout_confirmation_is_rejected_even_after_a_failed_first_attempt(
    recorder: TrialRecorder,
) -> None:
    """The one-shot seal must trip on ANY prior holdout_confirmation attempt,
    including one that errored -- a run that reads the sealed data and then
    errors has already consumed its single permitted look (§4.2)."""
    config = _config("2023-01-01", "2023-07-01")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    failing_validator = _mock_validator(exc=RuntimeError("holdout engine crash"))

    with pytest.raises(RuntimeError):
        recorder.run_walk_forward(
            failing_validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
            final_holdout_confirmation=True,
        )

    second_validator = _mock_validator(_wf_result())
    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            second_validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
            final_holdout_confirmation=True,
        )
    second_validator.run.assert_not_called()


def test_holdout_confirmation_requires_range_within_window(recorder: TrialRecorder) -> None:
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator()

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            # Starts before holdout_start -- not fully contained.
            config=_config("2022-11-01", "2023-03-01"),
            data_handler=MagicMock(),
            final_holdout_confirmation=True,
        )

    validator.run.assert_not_called()


def test_holdout_confirmation_requires_a_registered_window(recorder: TrialRecorder) -> None:
    _seed_definition(recorder)
    # No ResearchDataWindow seeded.
    validator = _mock_validator()

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            config=_config("2023-01-01", "2023-07-01"),
            data_handler=MagicMock(),
            final_holdout_confirmation=True,
        )

    validator.run.assert_not_called()


def test_holdout_seal_is_scoped_per_strategy_id(recorder: TrialRecorder) -> None:
    """Two distinct strategies must each be able to record their own
    holdout confirmation -- the seal must not be global."""
    config = _config("2023-01-01", "2023-07-01")
    config_hash = hash_config(config)
    _seed_definition(recorder, strategy_id="v1_alpha", config_hash=config_hash)
    _seed_definition(recorder, strategy_id="v1_beta", config_hash=config_hash)
    _seed_window(recorder, strategy_id="v1_alpha")
    _seed_window(recorder, strategy_id="v1_beta")

    validator_a = _mock_validator(_wf_result())
    validator_b = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator_a,
        strategy_id="v1_alpha",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
        final_holdout_confirmation=True,
    )
    recorder.run_walk_forward(
        validator_b,
        strategy_id="v1_beta",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
        final_holdout_confirmation=True,
    )

    assert len(recorder.list_trials("v1_alpha", run_type="holdout_confirmation")) == 1
    assert len(recorder.list_trials("v1_beta", run_type="holdout_confirmation")) == 1


# ── NaN/inf normalization ────────────────────────────────────────────────────────


def test_nan_and_inf_metrics_are_normalized_to_none(recorder: TrialRecorder) -> None:
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result(sharpe=float("nan"), max_dd=float("-inf")))

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
    )

    trials = recorder.list_trials("v1_test_strategy")
    assert len(trials) == 1
    trial = trials[0]
    assert trial.oos_sharpe is None
    assert trial.oos_max_drawdown is None
    # The full metrics bag must also have its non-finite leaf normalized,
    # not just the two first-class columns.
    assert trial.metrics_json["sharpe"] is None
    assert trial.metrics_json["max_drawdown"] is None
    assert trial.metrics_json["cagr"] == 0.15


# ── Definition / data_version guards ────────────────────────────────────────────


def test_missing_definition_raises_and_does_not_dispatch(recorder: TrialRecorder) -> None:
    _seed_window(recorder)
    validator = _mock_validator()

    with pytest.raises(DefinitionNotFoundError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            config=_config("2022-01-01", "2022-12-31"),
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()


def test_blank_data_version_is_rejected(recorder: TrialRecorder) -> None:
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator()

    with pytest.raises(MissingDataVersionError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version="   ",
            config=_config("2022-01-01", "2022-12-31"),
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()


# ── Parameter sweep happy path ───────────────────────────────────────────────────


def test_parameter_sweep_inside_train_oos_succeeds_and_records_one_row(recorder: TrialRecorder) -> None:
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper(_sweep_result(mean_sharpe=0.77, verdict="robust"))

    result = recorder.run_parameter_sweep(
        sweeper,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        base_config=base_config,
        param_grid={"portfolio.n_long": [10, 20]},
        data_handler=MagicMock(),
    )

    assert result.mean_oos_sharpe == 0.77
    sweeper.sweep.assert_called_once()

    trials = recorder.list_trials("v1_test_strategy", run_type="parameter_sweep_variant")
    assert len(trials) == 1
    trial = trials[0]
    assert trial.window == "train_oos"
    assert trial.status == "completed"
    assert float(trial.oos_sharpe) == 0.77
    assert trial.metrics_json["verdict"] == "robust"


# ── FIX 1: _mark_errored failure must not mask the original exception ──────────


def test_mark_errored_failure_does_not_mask_original_exception(recorder: TrialRecorder) -> None:
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(exc=RuntimeError("boom"))

    with patch.object(TrialRecorder, "_mark_errored", side_effect=RuntimeError("db blip during error handling")):
        with structlog.testing.capture_logs() as captured_logs:
            with pytest.raises(RuntimeError, match="boom"):
                recorder.run_walk_forward(
                    validator,
                    strategy_id="v1_test_strategy",
                    config_hash=config_hash,
                    data_version=DATA_VERSION,
                    config=config,
                    data_handler=MagicMock(),
                )

    events = [entry.get("event") for entry in captured_logs]
    assert "strategy_trial_error_recording_failed" in events
    failure_log = next(
        entry for entry in captured_logs if entry.get("event") == "strategy_trial_error_recording_failed"
    )
    assert "boom" in failure_log["original_error"]
    assert "db blip during error handling" in failure_log["recording_error"]
    # The 'errored' recording never happened, but the caller still saw the
    # ORIGINAL exception type/message, not the recording failure's.
    assert "strategy_trial_errored" not in events


# ── FIX 2: TOCTOU race on the holdout seal must raise HoldoutWindowViolationError,
# not a raw IntegrityError from uix_strategy_trials_one_holdout_confirmation ─────


def test_toctou_race_on_holdout_seal_is_translated_to_holdout_violation(
    recorder: TrialRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates two processes both passing _enforce_holdout_guard before
    either commits by bypassing the app-layer guard entirely -- the DB's
    partial unique index must then be the thing that actually stops the
    second insert, and _run_and_record must translate that raw IntegrityError
    into a clean HoldoutWindowViolationError rather than letting it leak.
    """
    config = _config("2023-01-01", "2023-07-01")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator_first = _mock_validator(_wf_result())
    validator_second = _mock_validator(_wf_result())

    monkeypatch.setattr(recorder, "_enforce_holdout_guard", lambda *args, **kwargs: None)

    recorder.run_walk_forward(
        validator_first,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
        final_holdout_confirmation=True,
    )
    validator_first.run.assert_called_once()

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator_second,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
            final_holdout_confirmation=True,
        )

    # The second run never reached dispatch -- the race was caught at the
    # Step 1 commit, before the wrapped instrument ran.
    validator_second.run.assert_not_called()
    trials = recorder.list_trials("v1_test_strategy", run_type="holdout_confirmation")
    assert len(trials) == 1


# ── FIX 3: reversed date range fails closed ─────────────────────────────────────


def test_reversed_date_range_raises_value_error(recorder: TrialRecorder) -> None:
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator()

    with pytest.raises(ValueError, match="reversed"):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            config=_config("2023-01-01", "2022-01-01"),
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


# ── FIX 1: a parameter sweep must never run against the sealed holdout ─────────


def test_parameter_sweep_final_holdout_confirmation_is_rejected_before_dispatch(
    recorder: TrialRecorder,
) -> None:
    """final_holdout_confirmation=True on run_parameter_sweep must fail
    closed BEFORE any recording or dispatch -- a sweep evaluates the whole
    param_grid, so running it against the sealed holdout would spend the
    one-shot seal on many looks at the holdout data rather than one fixed
    configuration."""
    _seed_definition(recorder)
    _seed_window(recorder)
    sweeper = _mock_sweeper(_sweep_result())

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            base_config=_config("2023-01-01", "2023-07-01"),
            param_grid={"portfolio.n_long": [10, 20]},
            data_handler=MagicMock(),
            final_holdout_confirmation=True,
        )

    sweeper.sweep.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


# ── FIX 2: C7 manifest-hash shape enforcement on data_version ──────────────────


@pytest.mark.parametrize(
    "bad_data_version",
    [
        "rqis-snapshots/manifests/2026-06-14/manifest.json",
        "not-a-hash",
        "2026-06-14",
        "A" * 64,  # uppercase hex fails the lowercase-only shape
    ],
)
def test_non_hash_data_version_is_rejected_on_walk_forward(
    recorder: TrialRecorder, bad_data_version: str
) -> None:
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator()

    with pytest.raises(ValueError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=bad_data_version,
            config=_config("2022-01-01", "2022-12-31"),
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_non_hash_data_version_is_rejected_on_parameter_sweep(recorder: TrialRecorder) -> None:
    _seed_definition(recorder)
    _seed_window(recorder)
    sweeper = _mock_sweeper()

    with pytest.raises(ValueError):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version="rqis-snapshots/manifests/2026-06-14/manifest.json",
            base_config=_config("2022-01-01", "2022-12-31"),
            param_grid={"portfolio.n_long": [10, 20]},
            data_handler=MagicMock(),
        )

    sweeper.sweep.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_valid_hash_shaped_data_version_is_accepted(recorder: TrialRecorder) -> None:
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version="3fae" + "0" * 60,
        config=config,
        data_handler=MagicMock(),
    )

    validator.run.assert_called_once()
    trials = recorder.list_trials("v1_test_strategy")
    assert len(trials) == 1
    assert trials[0].data_version == "3fae" + "0" * 60


# ── FIX 1 (P1): config-provenance check -- passed config must actually hash
# to the claimed config_hash, not merely have SOME row registered ──────────


def test_config_hash_mismatch_is_rejected_before_dispatch(recorder: TrialRecorder) -> None:
    """A caller that mutates the config (here: a different n_long) while
    reusing an already-registered config_hash must be rejected -- otherwise
    metrics would be recorded under a config_hash the passed config never
    actually produced, corrupting promotion evidence and DSR trial counts."""
    registered_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=registered_config)
    _seed_window(recorder)
    validator = _mock_validator()

    mutated_config = dict(registered_config)
    mutated_config["portfolio"] = {"n_long": 999}  # not part of the registered config

    with pytest.raises(ConfigProvenanceMismatchError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=mutated_config,
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_config_hash_match_is_accepted(recorder: TrialRecorder) -> None:
    """The mirror-image acceptance case: a config whose canonical hash
    genuinely equals the claimed config_hash proceeds normally."""
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
    )

    validator.run.assert_called_once()
    assert len(recorder.list_trials("v1_test_strategy")) == 1


def test_config_differing_only_in_data_version_is_not_a_mismatch(recorder: TrialRecorder) -> None:
    """``data_version`` is a runtime key excluded from the canonical hash
    (strategy_registry.fingerprint._RUNTIME_KEYS), so a config carrying a
    ``data_version`` that was absent when the strategy_definitions row was
    registered must NOT be flagged as a config_hash provenance mismatch.

    The carried ``data_version`` here is set to the SAME value as the
    ``data_version`` argument (``DATA_VERSION``) so this test isolates the
    config_hash check from the separate data_version-consistency check
    (FIX 2 / 04-2 round-2 P2) -- see
    ``test_config_data_version_differing_from_argument_is_rejected`` below
    for that check's own dedicated coverage.
    """
    registered_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=registered_config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result())

    config_with_data_version = dict(registered_config)
    # Absent at registration time; present now. Must be ignored by the hash.
    config_with_data_version["data_version"] = DATA_VERSION

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config_with_data_version,
        data_handler=MagicMock(),
    )

    validator.run.assert_called_once()
    assert len(recorder.list_trials("v1_test_strategy")) == 1


def test_parameter_sweep_config_hash_mismatch_is_rejected_before_dispatch(recorder: TrialRecorder) -> None:
    registered_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=registered_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper()

    mutated_base_config = dict(registered_config)
    mutated_base_config["portfolio"] = {"n_long": 999}

    with pytest.raises(ConfigProvenanceMismatchError):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=mutated_base_config,
            param_grid={"portfolio.n_long": [10, 20]},
            data_handler=MagicMock(),
        )

    sweeper.sweep.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


# ── FIX 2 (P1): non-confirmation runs must be CONTAINED in [train_start,
# oos_end], not merely avoid intersecting the holdout window ────────────────


def test_post_holdout_non_confirmation_run_is_rejected(recorder: TrialRecorder) -> None:
    """A non-confirmation run entirely AFTER holdout_end does not intersect
    the holdout window under the old overlap-only check, but it is still a
    peek at post-holdout data and must be rejected under containment."""
    config = _config("2023-08-01", "2023-12-31")  # after holdout_end (2023-07-01)
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)  # oos_end=2023-01-01, holdout_end=2023-07-01
    validator = _mock_validator()

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_pre_train_start_non_confirmation_run_is_rejected(recorder: TrialRecorder) -> None:
    """A non-confirmation run entirely BEFORE train_start is the same class
    of leak (data outside the registered train/OOS partition) and must be
    rejected under containment even though it never touches the holdout."""
    config = _config("2021-01-01", "2021-06-01")  # before train_start (2022-01-01)
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator()

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_run_entirely_within_train_oos_partition_is_accepted(recorder: TrialRecorder) -> None:
    """The positive case: a range fully inside [train_start, oos_end] and
    strictly before holdout_start is accepted -- containment must not be
    stricter than necessary for genuinely in-bounds runs.

    Ends one day before the default seeded window's oos_end/holdout_start
    (both 2023-01-01 -- see ``_seed_window``'s touching-partition default)
    rather than exactly on it, since FIX 2 (04-2 round-2 P1) now rejects a
    run whose effective_end lands exactly on that shared boundary date --
    see ``test_run_ending_exactly_on_touching_holdout_boundary_is_rejected``.
    """
    config = _config("2022-01-01", "2022-12-31")  # inside partition, before holdout_start
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
    )

    validator.run.assert_called_once()
    assert len(recorder.list_trials("v1_test_strategy")) == 1


def test_confirmation_mode_still_requires_containment_in_holdout_window_only(
    recorder: TrialRecorder,
) -> None:
    """Confirmation-mode behavior must be unchanged by FIX 2: it still checks
    containment in [holdout_start, holdout_end] specifically, not
    [train_start, oos_end] -- a confirmation range that is inside train/OOS
    but NOT inside the holdout window must still be rejected."""
    config = _config("2022-06-01", "2022-12-01")  # inside train/OOS, not holdout
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator()

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
            final_holdout_confirmation=True,
        )

    validator.run.assert_not_called()


def test_no_registered_window_advisory_mode_allows_any_range(recorder: TrialRecorder) -> None:
    """Hybrid mode (design doc §8 Q4): when no research_data_windows row is
    registered for the strategy (or its family), the recorder cannot know
    the train/OOS/holdout partition, so a non-confirmation run is not
    constrained at all -- this must remain true after FIX 2."""
    config = _config("2099-01-01", "2099-12-31")  # arbitrary; no window to violate
    config_hash = _seed_definition(recorder, config=config)
    # Deliberately no _seed_window call.
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
    )

    validator.run.assert_called_once()
    assert len(recorder.list_trials("v1_test_strategy")) == 1


# ── FIX 1 (04-2 round-2 P1): touching oos_end/holdout_start boundary must be
# excluded from the non-confirmation containment window -- backtest date
# ranges are inclusive, so a run ending exactly on a shared boundary date
# would otherwise read the first sealed holdout session ─────────────────────


def test_run_ending_exactly_on_touching_holdout_boundary_is_rejected(
    recorder: TrialRecorder,
) -> None:
    """The default seeded window has oos_end == holdout_start == 2023-01-01
    (touching partitions, permitted by the 04-1 ordering CHECK). A
    non-confirmation run ending exactly on that shared boundary date reads
    the first sealed holdout session (dates are inclusive) while still being
    recorded as window='train_oos' -- a leak that the old
    `effective_end <= window.oos_end` check alone did not catch."""
    config = _config("2022-01-01", "2023-01-01")  # ends exactly on oos_end == holdout_start
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)  # oos_end=2023-01-01, holdout_start=2023-01-01 (touching)
    validator = _mock_validator()

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_run_ending_one_day_before_touching_holdout_boundary_is_accepted(
    recorder: TrialRecorder,
) -> None:
    """The mirror-image positive case: a run ending one day before the same
    touching oos_end/holdout_start boundary (2022-12-31 vs. 2023-01-01) is
    accepted -- FIX 1 must not reject runs that genuinely stay inside the
    train/OOS partition and strictly before the holdout seal."""
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)  # oos_end=2023-01-01, holdout_start=2023-01-01 (touching)
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
    )

    validator.run.assert_called_once()
    assert len(recorder.list_trials("v1_test_strategy")) == 1


def test_gap_window_run_ending_before_holdout_start_is_still_accepted(
    recorder: TrialRecorder,
) -> None:
    """Regression guard for the non-touching (gap) case: when oos_end <
    holdout_start, a run fully inside [train_start, oos_end] must still be
    accepted -- FIX 1's added `effective_end < window.holdout_start` clause
    is implied by (not stricter than) `effective_end <= window.oos_end` when
    there is a gap, so this must behave exactly as before."""
    config = _config("2022-01-01", "2022-11-01")  # inside [train_start, oos_end], gap before holdout
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(
        recorder,
        oos_end=date(2022, 12, 1),
        holdout_start=date(2023, 1, 1),  # gap between oos_end and holdout_start
        holdout_end=date(2023, 7, 1),
    )
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
    )

    validator.run.assert_called_once()
    assert len(recorder.list_trials("v1_test_strategy")) == 1


# ── FIX 2 (04-2 round-2 P2): dispatched config's data_version must match the
# recorded data_version ──────────────────────────────────────────────────────


def test_config_data_version_differing_from_argument_is_rejected(recorder: TrialRecorder) -> None:
    """A config whose top-level data_version is non-empty and differs from
    the data_version argument must be rejected before dispatch -- a caller
    should not silently disagree with itself about which C7 data snapshot a
    trial ran against."""
    config = _config("2022-01-01", "2022-12-31")
    config["data_version"] = "b" * 64
    config_hash = _seed_definition(
        recorder,
        config={k: v for k, v in config.items() if k != "data_version"},
    )
    _seed_window(recorder)
    validator = _mock_validator()

    with pytest.raises(DataVersionProvenanceMismatchError):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,  # "a" * 64 -- differs from config["data_version"]
            config=config,
            data_handler=MagicMock(),
        )

    validator.run.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_config_without_data_version_gets_the_validated_value_dispatched(
    recorder: TrialRecorder,
) -> None:
    """A config with no top-level data_version is accepted, and the config
    object actually dispatched to the wrapped instrument carries the
    validated data_version argument -- proving TrialRecorder injects it
    rather than dispatching the caller's config unchanged."""
    config = _config("2022-01-01", "2022-12-31")  # no data_version key
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
    )

    validator.run.assert_called_once()
    dispatched_config = validator.run.call_args.args[0]
    assert dispatched_config["data_version"] == DATA_VERSION
    # The caller's original config dict must be left untouched.
    assert "data_version" not in config


def test_recorded_trial_data_version_matches_what_the_instrument_was_dispatched_with(
    recorder: TrialRecorder,
) -> None:
    """The strategy_trials.data_version column must equal the data_version
    the (mocked) instrument actually received in its dispatched config --
    the whole point of FIX 2 is that these two can never disagree."""
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
    )

    dispatched_config = validator.run.call_args.args[0]
    recorded = recorder.list_trials("v1_test_strategy")[0]
    assert dispatched_config["data_version"] == recorded.data_version == DATA_VERSION


def test_parameter_sweep_dispatched_config_carries_validated_data_version(
    recorder: TrialRecorder,
) -> None:
    """Same FIX 2 propagation, exercised through run_parameter_sweep: the
    base_config actually dispatched to sweeper.sweep() must carry the
    validated data_version, not the caller's unmodified base_config."""
    base_config = _config("2022-01-01", "2022-12-31")  # no data_version key
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper(_sweep_result())

    recorder.run_parameter_sweep(
        sweeper,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        base_config=base_config,
        param_grid={"portfolio.n_long": [10, 20]},
        data_handler=MagicMock(),
    )

    sweeper.sweep.assert_called_once()
    dispatched_config = sweeper.sweep.call_args.args[0]
    assert dispatched_config["data_version"] == DATA_VERSION


# ── R4 fix: param_grid may never override the evaluation date window ───────────
#
# Four consecutive Codex review rounds each found a different instance of ONE
# defect class in this holdout/partition guard: the guard must validate every
# concrete date range whose data is actually READ during dispatch, never a
# declared/base range dispatch can diverge from. R2 fixed base-range
# containment; R3 fixed the inclusive touching-boundary gap; these tests
# cover R4: ParameterSweeper.sweep applies param_grid dot-path overrides
# per-variant, so a param_grid containing backtest.start_date/end_date could
# pass the guard on a validated BASE range while a dispatched VARIANT reads
# sealed holdout / post-holdout data, recorded under a single 'train_oos' row
# with the one-shot seal never consumed.


def test_parameter_sweep_rejects_start_date_override_before_dispatch(
    recorder: TrialRecorder,
) -> None:
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)  # holdout = 2023-01-01 .. 2023-07-01
    sweeper = _mock_sweeper(_sweep_result())

    with pytest.raises(SweepWindowOverrideError, match="backtest.start_date"):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=base_config,
            param_grid={"backtest.start_date": ["2022-06-01", "2023-02-01"]},
            data_handler=MagicMock(),
        )

    sweeper.sweep.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_parameter_sweep_rejects_end_date_override_before_dispatch(
    recorder: TrialRecorder,
) -> None:
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)  # holdout = 2023-01-01 .. 2023-07-01

    # This variant value (2023-03-01) is inside the sealed holdout window --
    # exactly the leak R4 exists to close: the base range (2022-01-01 ..
    # 2022-12-31) alone passes the §4.2 guard, but a dispatched variant using
    # this override would read holdout data outside any validated range.
    sweeper = _mock_sweeper(_sweep_result())

    with pytest.raises(SweepWindowOverrideError, match="backtest.end_date"):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=base_config,
            param_grid={"backtest.end_date": ["2022-12-31", "2023-03-01"]},
            data_handler=MagicMock(),
        )

    sweeper.sweep.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_parameter_sweep_rejects_both_window_keys_listed_together(
    recorder: TrialRecorder,
) -> None:
    """Both offending keys must be reported in one error, not just the first
    one found -- mirrors validate_backtest_config's "collect every violation"
    convention elsewhere in this codebase."""
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper(_sweep_result())

    with pytest.raises(SweepWindowOverrideError) as excinfo:
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=base_config,
            param_grid={
                "backtest.start_date": ["2022-06-01"],
                "backtest.end_date": ["2023-03-01"],
                "portfolio.n_long": [10, 20],
            },
            data_handler=MagicMock(),
        )

    assert "backtest.start_date" in str(excinfo.value)
    assert "backtest.end_date" in str(excinfo.value)
    sweeper.sweep.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_parameter_sweep_window_override_rejected_even_without_a_registered_window(
    recorder: TrialRecorder,
) -> None:
    """The window-override rejection must fire even when no
    research_data_windows row is registered at all -- it is not conditioned
    on a holdout window existing, because the defect it closes (a variant
    reading dates outside the validated base range) is independent of
    whether that range happens to touch a holdout window today."""
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    sweeper = _mock_sweeper(_sweep_result())

    with pytest.raises(SweepWindowOverrideError):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=base_config,
            param_grid={"backtest.start_date": ["2021-01-01"]},
            data_handler=MagicMock(),
        )

    sweeper.sweep.assert_not_called()


def test_parameter_sweep_legitimate_strategy_param_grid_still_accepted(
    recorder: TrialRecorder,
) -> None:
    """A param_grid that only overrides legitimate STRATEGY parameters (e.g.
    portfolio.n_long) must still run normally -- the R4 fix rejects
    date-window keys specifically, not param_grid sweeps in general."""
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper(_sweep_result(mean_sharpe=0.9))

    result = recorder.run_parameter_sweep(
        sweeper,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        base_config=base_config,
        param_grid={
            "portfolio.n_long": [10, 20, 30],
            "portfolio.min_holding_days": [0, 21],
        },
        data_handler=MagicMock(),
    )

    assert result.mean_oos_sharpe == 0.9
    sweeper.sweep.assert_called_once()
    trials = recorder.list_trials("v1_test_strategy")
    assert len(trials) == 1
    assert trials[0].status == "completed"
    assert trials[0].run_type == "parameter_sweep_variant"


def test_parameter_sweep_window_override_checked_before_final_holdout_confirmation_check(
    recorder: TrialRecorder,
) -> None:
    """When both a window-override param_grid AND
    final_holdout_confirmation=True are passed, the window-override rejection
    must win (it is checked first) -- both are fail-closed no-dispatch paths,
    but the error message should point at the actual mistake (an unsupported
    param_grid key) rather than the unrelated one-shot-seal message."""
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper(_sweep_result())

    with pytest.raises(SweepWindowOverrideError):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=base_config,
            param_grid={"backtest.start_date": ["2022-06-01"]},
            data_handler=MagicMock(),
            final_holdout_confirmation=True,
        )

    sweeper.sweep.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


# Codex round-5 P1: ParameterSweeper._set_nested applies a param_grid key as a
# dot-path and REPLACES the whole subtree at that path -- not just a scalar
# leaf. So rejecting only the exact leaf keys ("backtest.start_date",
# "backtest.end_date") left a gap: a param_grid key of "backtest" (the whole
# section) also replaces both dates wholesale, evading the exact-key filter.
# These tests cover the ancestry-based rejection rule that closes it, and
# prove the rule is precise (siblings like backtest.initial_capital are still
# allowed) rather than a blanket ban on every backtest.* key.


def test_parameter_sweep_rejects_whole_backtest_section_override_before_dispatch(
    recorder: TrialRecorder,
) -> None:
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)  # holdout = 2023-01-01 .. 2023-07-01
    sweeper = _mock_sweeper(_sweep_result())

    with pytest.raises(SweepWindowOverrideError, match="backtest"):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=base_config,
            param_grid={
                "backtest": [
                    {
                        "start_date": "2023-03-01",
                        "end_date": "2023-06-01",
                        "initial_capital": 100000,
                    }
                ]
            },
            data_handler=MagicMock(),
        )

    sweeper.sweep.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_parameter_sweep_rejects_empty_string_key(recorder: TrialRecorder) -> None:
    """A blank/whitespace-only param_grid key is a degenerate ancestor of the
    entire config root and must be rejected defensively, not silently
    forwarded to ParameterSweeper._set_nested."""
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper(_sweep_result())

    with pytest.raises(SweepWindowOverrideError):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            base_config=base_config,
            param_grid={"": [1, 2]},
            data_handler=MagicMock(),
        )

    sweeper.sweep.assert_not_called()
    assert recorder.list_trials("v1_test_strategy") == []


def test_parameter_sweep_accepts_initial_capital_sibling_key(
    recorder: TrialRecorder,
) -> None:
    """backtest.initial_capital is a sibling of backtest.start_date/end_date,
    not an ancestor of either -- the ancestry rule must accept it so
    legitimate non-window sweeps under the backtest section still work
    (proves the fix is precise, not a blanket ban on all backtest.* keys)."""
    base_config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=base_config)
    _seed_window(recorder)
    sweeper = _mock_sweeper(_sweep_result(mean_sharpe=0.75))

    result = recorder.run_parameter_sweep(
        sweeper,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        base_config=base_config,
        param_grid={"backtest.initial_capital": [50000, 100000]},
        data_handler=MagicMock(),
    )

    assert result.mean_oos_sharpe == 0.75
    sweeper.sweep.assert_called_once()
    trials = recorder.list_trials("v1_test_strategy")
    assert len(trials) == 1
    assert trials[0].status == "completed"
    assert trials[0].run_type == "parameter_sweep_variant"


# ── Walk-forward fold containment (proof the class is closed for folds) ────────


def test_walk_forward_folds_never_exceed_the_validated_outer_range() -> None:
    """WalkForwardValidator._build_fold_dates must only ever produce fold
    date tuples drawn from the outer [full_start, full_end] range that
    TrialRecorder's §4.2 guard validated -- this is why fold subdivision
    needs no separate holdout check (see the assertion/comment added to
    WalkForwardValidator.run alongside this test)."""
    from backtesting.validation.walk_forward import _build_fold_dates
    import pandas as pd

    full_start = date(2020, 1, 1)
    full_end = date(2023, 12, 31)
    all_dates = list(pd.bdate_range(full_start, full_end).date)

    folds = _build_fold_dates(
        all_dates, n_folds=3, train_years=2.0, test_months=6, window_type="expanding"
    )

    assert folds, "expected at least one fold for this date range"
    for tr_start, tr_end, te_start, te_end in folds:
        assert full_start <= tr_start <= tr_end <= te_start <= te_end <= full_end


# ── 04-3: frozen_at freeze-on-first-trial side effect ────────────────────────────


def test_legacy_path_hypothesis_id_none_records_trial_with_no_freeze_and_no_error(
    recorder: TrialRecorder,
) -> None:
    """The pre-04-3 legacy path (hypothesis_id=None) is unaffected: no
    strategy_hypotheses row is touched, and recording succeeds exactly as
    before."""
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result(sharpe=1.1))

    result = recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
        hypothesis_id=None,
    )

    assert result.oos_metrics["sharpe"] == 1.1
    trials = recorder.list_trials("v1_test_strategy")
    assert len(trials) == 1
    assert trials[0].hypothesis_id is None


def test_recording_trial_with_unfrozen_hypothesis_freezes_it_as_side_effect(
    recorder: TrialRecorder,
) -> None:
    """A trial linking a hypothesis whose frozen_at is still NULL freezes it
    (sets frozen_at) as a side effect of recording the trial (§5.1, §7 row
    04-3 acceptance evidence)."""
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    hypothesis_id = _seed_hypothesis(recorder)
    validator = _mock_validator(_wf_result(sharpe=1.3))

    with Session(recorder._engine) as session:
        before = session.get(StrategyHypothesis, hypothesis_id)
        assert before.frozen_at is None

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
        hypothesis_id=hypothesis_id,
    )

    with Session(recorder._engine) as session:
        after = session.get(StrategyHypothesis, hypothesis_id)
        assert after.frozen_at is not None

    trials = recorder.list_trials("v1_test_strategy")
    assert len(trials) == 1
    assert trials[0].hypothesis_id == hypothesis_id


def test_second_trial_linking_already_frozen_hypothesis_is_a_noop_on_frozen_at(
    recorder: TrialRecorder,
) -> None:
    """A second trial linking an already-frozen hypothesis proceeds without
    error and does not change frozen_at (it is set exactly once, on the
    first linked trial)."""
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    hypothesis_id = _seed_hypothesis(recorder)
    validator = _mock_validator(_wf_result(sharpe=1.0))

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
        hypothesis_id=hypothesis_id,
    )
    with Session(recorder._engine) as session:
        first_frozen_at = session.get(StrategyHypothesis, hypothesis_id).frozen_at
    assert first_frozen_at is not None

    # Second trial, same hypothesis -- must succeed and leave frozen_at
    # unchanged (not bumped to a later timestamp).
    validator2 = _mock_validator(_wf_result(sharpe=2.0))
    recorder.run_walk_forward(
        validator2,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
        hypothesis_id=hypothesis_id,
    )

    with Session(recorder._engine) as session:
        second_frozen_at = session.get(StrategyHypothesis, hypothesis_id).frozen_at
    assert second_frozen_at == first_frozen_at

    trials = recorder.list_trials("v1_test_strategy")
    assert len(trials) == 2
    assert all(t.hypothesis_id == hypothesis_id for t in trials)


def test_freeze_uses_conditional_update_and_never_overwrites_an_existing_timestamp(
    recorder: TrialRecorder,
) -> None:
    """Codex round-1 P2: the freeze side effect is an atomic conditional
    UPDATE (`... WHERE frozen_at IS NULL`), so a hypothesis already frozen at
    a KNOWN earlier timestamp is never overwritten by a later linked trial,
    and that later trial emits NO `strategy_hypothesis_frozen` event -- the
    set-once invariant and the audit timestamp both belong to the first
    linked trial, not whichever write commits last."""
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    hypothesis_id = _seed_hypothesis(recorder)

    # Pre-freeze directly to a distinct sentinel timestamp (simulating a first
    # writer that already won the set-once race).
    sentinel = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with Session(recorder._engine) as session:
        hyp = session.get(StrategyHypothesis, hypothesis_id)
        hyp.frozen_at = sentinel
        session.add(hyp)
        session.commit()

    with structlog.testing.capture_logs() as logs:
        recorder.run_walk_forward(
            _mock_validator(_wf_result(sharpe=1.0)),
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
            hypothesis_id=hypothesis_id,
        )

    with Session(recorder._engine) as session:
        after = session.get(StrategyHypothesis, hypothesis_id).frozen_at
    # frozen_at is unchanged (conditional UPDATE matched 0 rows), and no
    # freeze event was emitted for this (non-first) linked trial. Compare
    # tz-naive: SQLite returns naive datetimes, so the point is that the
    # stored instant is still the 2020 sentinel, NOT the ~current trial
    # started_at that a non-conditional overwrite would have written.
    assert after.replace(tzinfo=None) == sentinel.replace(tzinfo=None)
    assert not any(e.get("event") == "strategy_hypothesis_frozen" for e in logs)


def test_freeze_log_not_emitted_when_trial_insert_fails_after_freeze(
    recorder: TrialRecorder,
) -> None:
    """FIX 3: if the in-session freeze is applied but the trial insert then
    fails (here: the composite (hypothesis_id, strategy_id) FK rejects a
    hypothesis registered under a DIFFERENT strategy_id than the trial),
    the whole transaction rolls back -- frozen_at must stay NULL AND the
    'strategy_hypothesis_frozen' audit log must never have been emitted,
    since logging before commit would otherwise record a freeze that never
    took effect."""
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, strategy_id="v2_other_strategy", config=config)
    _seed_window(recorder, strategy_id="v2_other_strategy")
    # Hypothesis registered for a DIFFERENT strategy_id -> composite FK
    # mismatch when linked to a v2_other_strategy trial.
    hypothesis_id = _seed_hypothesis(recorder, strategy_id="v1_test_strategy")
    validator = _mock_validator(_wf_result(sharpe=0.9))

    with structlog.testing.capture_logs() as captured_logs:
        with pytest.raises(Exception):
            recorder.run_walk_forward(
                validator,
                strategy_id="v2_other_strategy",
                config_hash=config_hash,
                data_version=DATA_VERSION,
                config=config,
                data_handler=MagicMock(),
                hypothesis_id=hypothesis_id,
            )

    events = [entry.get("event") for entry in captured_logs]
    assert "strategy_hypothesis_frozen" not in events

    with Session(recorder._engine) as session:
        hyp = session.get(StrategyHypothesis, hypothesis_id)
        assert hyp.frozen_at is None


def test_freeze_log_emitted_exactly_once_after_successful_commit(
    recorder: TrialRecorder,
) -> None:
    """FIX 3: a successful first linked trial DOES emit exactly one
    'strategy_hypothesis_frozen' log line, and only after the commit that
    durably applies the freeze."""
    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    hypothesis_id = _seed_hypothesis(recorder)
    validator = _mock_validator(_wf_result(sharpe=1.5))

    with structlog.testing.capture_logs() as captured_logs:
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash=config_hash,
            data_version=DATA_VERSION,
            config=config,
            data_handler=MagicMock(),
            hypothesis_id=hypothesis_id,
        )

    events = [entry.get("event") for entry in captured_logs]
    assert events.count("strategy_hypothesis_frozen") == 1

    with Session(recorder._engine) as session:
        hyp = session.get(StrategyHypothesis, hypothesis_id)
        assert hyp.frozen_at is not None


def test_param_grid_edit_rejected_after_recorder_freezes_hypothesis(
    recorder: TrialRecorder, db_url: str
) -> None:
    """End-to-end: registering via HypothesisRegistry, freezing via
    TrialRecorder (same DB), then confirming HypothesisRegistry.
    update_param_grid rejects a post-freeze edit with the immutability
    error."""
    from strategy_registry.hypothesis import (
        HypothesisParamGridFrozenError,
        HypothesisRegistry,
    )

    hyp_registry = HypothesisRegistry(db_url)
    hyp = hyp_registry.register_hypothesis(
        strategy_id="v1_test_strategy",
        hypothesis_text="Momentum window sensitivity",
        param_grid_json={"momentum_window": [3, 6]},
    )
    assert hyp.frozen_at is None

    # Editing while unfrozen is allowed.
    hyp = hyp_registry.update_param_grid(hyp.id, {"momentum_window": [3, 6, 12]})
    assert hyp.frozen_at is None

    config = _config("2022-01-01", "2022-12-31")
    config_hash = _seed_definition(recorder, config=config)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result(sharpe=1.4))

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash=config_hash,
        data_version=DATA_VERSION,
        config=config,
        data_handler=MagicMock(),
        hypothesis_id=hyp.id,
    )

    frozen = hyp_registry.get_hypothesis(hyp.id)
    assert frozen.frozen_at is not None

    with pytest.raises(HypothesisParamGridFrozenError):
        hyp_registry.update_param_grid(hyp.id, {"momentum_window": [3, 6, 12, 24]})

    # Grid is unchanged after the rejected edit.
    unchanged = hyp_registry.get_hypothesis(hyp.id)
    assert unchanged.param_grid_json == {"momentum_window": [3, 6, 12]}
