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

from backtesting.validation.parameter_sensitivity import (
    ParameterSensitivityResult,
    ParameterSweeper,
)
from backtesting.validation.trial_recorder import (
    HoldoutWindowViolationError,
    TrialRecorder,
)
from backtesting.validation.walk_forward import WalkForwardResult, WalkForwardValidator
from strategy_registry.registry import DefinitionNotFoundError, MissingDataVersionError
from strategy_registry.models import StrategyDefinition
from strategy_registry.selection_models import ResearchDataWindow
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
    config_hash: str = "a" * 64,
) -> None:
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
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result(sharpe=1.5, max_dd=-0.12))

    result = recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash="a" * 64,
        data_version=DATA_VERSION,
        config=_config("2022-01-01", "2022-12-31"),
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
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator(exc=RuntimeError("engine blew up mid-fold"))

    with pytest.raises(RuntimeError, match="engine blew up mid-fold"):
        recorder.run_walk_forward(
            validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            config=_config("2022-01-01", "2022-12-31"),
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
    _seed_definition(recorder)
    _seed_window(recorder)
    sweeper = _mock_sweeper(exc=ValueError("param_grid must not be empty."))

    with pytest.raises(ValueError):
        recorder.run_parameter_sweep(
            sweeper,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            base_config=_config("2022-01-01", "2022-12-31"),
            param_grid={"portfolio.n_long": [10, 20]},
            data_handler=MagicMock(),
        )

    trials = recorder.list_trials("v1_test_strategy", run_type="parameter_sweep_variant")
    assert len(trials) == 1
    assert trials[0].status == "errored"


# ── Holdout confirmation: one-shot seal ─────────────────────────────────────────


def test_first_holdout_confirmation_succeeds_and_records_holdout_row(recorder: TrialRecorder) -> None:
    _seed_definition(recorder)
    _seed_window(recorder)  # holdout = 2023-01-01 .. 2023-07-01
    validator = _mock_validator(_wf_result(sharpe=0.9, max_dd=-0.2))

    result = recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash="a" * 64,
        data_version=DATA_VERSION,
        config=_config("2023-01-01", "2023-07-01"),
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
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash="a" * 64,
        data_version=DATA_VERSION,
        config=_config("2023-01-01", "2023-07-01"),
        data_handler=MagicMock(),
        final_holdout_confirmation=True,
    )
    assert validator.run.call_count == 1

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
    _seed_definition(recorder)
    _seed_window(recorder)
    failing_validator = _mock_validator(exc=RuntimeError("holdout engine crash"))

    with pytest.raises(RuntimeError):
        recorder.run_walk_forward(
            failing_validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            config=_config("2023-01-01", "2023-07-01"),
            data_handler=MagicMock(),
            final_holdout_confirmation=True,
        )

    second_validator = _mock_validator(_wf_result())
    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            second_validator,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            config=_config("2023-01-01", "2023-07-01"),
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
    _seed_definition(recorder, strategy_id="v1_alpha", config_hash="a" * 64)
    _seed_definition(recorder, strategy_id="v1_beta", config_hash="b" * 64)
    _seed_window(recorder, strategy_id="v1_alpha")
    _seed_window(recorder, strategy_id="v1_beta")

    validator_a = _mock_validator(_wf_result())
    validator_b = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator_a,
        strategy_id="v1_alpha",
        config_hash="a" * 64,
        data_version=DATA_VERSION,
        config=_config("2023-01-01", "2023-07-01"),
        data_handler=MagicMock(),
        final_holdout_confirmation=True,
    )
    recorder.run_walk_forward(
        validator_b,
        strategy_id="v1_beta",
        config_hash="b" * 64,
        data_version=DATA_VERSION,
        config=_config("2023-01-01", "2023-07-01"),
        data_handler=MagicMock(),
        final_holdout_confirmation=True,
    )

    assert len(recorder.list_trials("v1_alpha", run_type="holdout_confirmation")) == 1
    assert len(recorder.list_trials("v1_beta", run_type="holdout_confirmation")) == 1


# ── NaN/inf normalization ────────────────────────────────────────────────────────


def test_nan_and_inf_metrics_are_normalized_to_none(recorder: TrialRecorder) -> None:
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result(sharpe=float("nan"), max_dd=float("-inf")))

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash="a" * 64,
        data_version=DATA_VERSION,
        config=_config("2022-01-01", "2022-12-31"),
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
    _seed_definition(recorder)
    _seed_window(recorder)
    sweeper = _mock_sweeper(_sweep_result(mean_sharpe=0.77, verdict="robust"))

    result = recorder.run_parameter_sweep(
        sweeper,
        strategy_id="v1_test_strategy",
        config_hash="a" * 64,
        data_version=DATA_VERSION,
        base_config=_config("2022-01-01", "2022-12-31"),
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
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator(exc=RuntimeError("boom"))

    with patch.object(TrialRecorder, "_mark_errored", side_effect=RuntimeError("db blip during error handling")):
        with structlog.testing.capture_logs() as captured_logs:
            with pytest.raises(RuntimeError, match="boom"):
                recorder.run_walk_forward(
                    validator,
                    strategy_id="v1_test_strategy",
                    config_hash="a" * 64,
                    data_version=DATA_VERSION,
                    config=_config("2022-01-01", "2022-12-31"),
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
    _seed_definition(recorder)
    _seed_window(recorder)
    validator_first = _mock_validator(_wf_result())
    validator_second = _mock_validator(_wf_result())

    monkeypatch.setattr(recorder, "_enforce_holdout_guard", lambda *args, **kwargs: None)

    recorder.run_walk_forward(
        validator_first,
        strategy_id="v1_test_strategy",
        config_hash="a" * 64,
        data_version=DATA_VERSION,
        config=_config("2023-01-01", "2023-07-01"),
        data_handler=MagicMock(),
        final_holdout_confirmation=True,
    )
    validator_first.run.assert_called_once()

    with pytest.raises(HoldoutWindowViolationError):
        recorder.run_walk_forward(
            validator_second,
            strategy_id="v1_test_strategy",
            config_hash="a" * 64,
            data_version=DATA_VERSION,
            config=_config("2023-01-01", "2023-07-01"),
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
    _seed_definition(recorder)
    _seed_window(recorder)
    validator = _mock_validator(_wf_result())

    recorder.run_walk_forward(
        validator,
        strategy_id="v1_test_strategy",
        config_hash="a" * 64,
        data_version="3fae" + "0" * 60,
        config=_config("2022-01-01", "2022-12-31"),
        data_handler=MagicMock(),
    )

    validator.run.assert_called_once()
    trials = recorder.list_trials("v1_test_strategy")
    assert len(trials) == 1
    assert trials[0].data_version == "3fae" + "0" * 60
