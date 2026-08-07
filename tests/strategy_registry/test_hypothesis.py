"""Tests for the hypothesis write path (Gate 04 slice 04-3,
docs/plans/04-strategy-selection-protocol-design.md §4.1, §5.1, §7 row 04-3).

Follows the SQLite + ``PRAGMA foreign_keys=ON`` convention established by
``tests/strategy_registry/test_selection_models.py`` and
``backtesting/tests/test_trial_recorder.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from strategy_registry.hypothesis import (
    HypothesisNotFoundError,
    HypothesisParamGridFrozenError,
    HypothesisRegistry,
    InvalidHypothesisError,
)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'hypothesis.db'}"


@pytest.fixture
def hyp_registry(db_url: str) -> HypothesisRegistry:
    return HypothesisRegistry(db_url)


# ── register_hypothesis ───────────────────────────────────────────────────────


def test_register_creates_row_with_null_frozen_at(hyp_registry: HypothesisRegistry) -> None:
    grid = {"momentum_window": [3, 6, 12]}
    hyp = hyp_registry.register_hypothesis(
        strategy_id="v1_base_momentum",
        hypothesis_text="Momentum window sensitivity",
        param_grid_json=grid,
    )
    assert hyp.id is not None
    assert hyp.strategy_id == "v1_base_momentum"
    assert hyp.hypothesis_text == "Momentum window sensitivity"
    assert hyp.param_grid_json == grid
    assert hyp.frozen_at is None
    assert hyp.created_at is not None


def test_register_rejects_empty_hypothesis_text(hyp_registry: HypothesisRegistry) -> None:
    with pytest.raises(InvalidHypothesisError):
        hyp_registry.register_hypothesis(
            strategy_id="v1_base_momentum",
            hypothesis_text="   ",
        )


def test_register_rejects_malformed_strategy_id(hyp_registry: HypothesisRegistry) -> None:
    with pytest.raises(InvalidHypothesisError):
        hyp_registry.register_hypothesis(
            strategy_id="Bad Id!",
            hypothesis_text="anything",
        )


def test_register_allows_none_param_grid(hyp_registry: HypothesisRegistry) -> None:
    hyp = hyp_registry.register_hypothesis(
        strategy_id="v1_base_momentum",
        hypothesis_text="No grid yet",
    )
    assert hyp.param_grid_json is None
    assert hyp.frozen_at is None


# ── update_param_grid (immutability) ───────────────────────────────────────────


def test_update_param_grid_allowed_while_unfrozen(hyp_registry: HypothesisRegistry) -> None:
    hyp = hyp_registry.register_hypothesis(
        strategy_id="v1_base_momentum",
        hypothesis_text="Momentum window sensitivity",
        param_grid_json={"momentum_window": [3, 6]},
    )
    updated = hyp_registry.update_param_grid(hyp.id, {"momentum_window": [3, 6, 12]})
    assert updated.param_grid_json == {"momentum_window": [3, 6, 12]}
    assert updated.frozen_at is None


def test_update_param_grid_rejects_unknown_id(hyp_registry: HypothesisRegistry) -> None:
    with pytest.raises(HypothesisNotFoundError):
        hyp_registry.update_param_grid(999999, {"x": [1]})


def test_get_hypothesis_rejects_unknown_id(hyp_registry: HypothesisRegistry) -> None:
    with pytest.raises(HypothesisNotFoundError):
        hyp_registry.get_hypothesis(999999)


def test_list_hypotheses(hyp_registry: HypothesisRegistry) -> None:
    hyp_registry.register_hypothesis(strategy_id="v1_base_momentum", hypothesis_text="one")
    hyp_registry.register_hypothesis(strategy_id="v1_base_momentum", hypothesis_text="two")
    hyp_registry.register_hypothesis(strategy_id="v2_other", hypothesis_text="unrelated")
    rows = hyp_registry.list_hypotheses("v1_base_momentum")
    assert len(rows) == 2
    assert {r.hypothesis_text for r in rows} == {"one", "two"}


def test_update_param_grid_rejected_after_frozen(
    hyp_registry: HypothesisRegistry, db_url: str
) -> None:
    """Freezing is exercised via TrialRecorder in
    backtesting/tests/test_trial_recorder.py; here we simulate the frozen
    state directly against the same DB to prove update_param_grid's
    immutability check independent of TrialRecorder wiring.
    """
    from datetime import datetime, timezone

    from sqlalchemy.orm import Session

    hyp = hyp_registry.register_hypothesis(
        strategy_id="v1_base_momentum",
        hypothesis_text="Momentum window sensitivity",
        param_grid_json={"momentum_window": [3, 6]},
    )
    with Session(hyp_registry._engine) as session:
        from strategy_registry.selection_models import StrategyHypothesis

        row = session.get(StrategyHypothesis, hyp.id)
        row.frozen_at = datetime.now(timezone.utc)
        session.commit()

    with pytest.raises(HypothesisParamGridFrozenError):
        hyp_registry.update_param_grid(hyp.id, {"momentum_window": [3, 6, 12, 24]})

    # Grid must remain unchanged after the rejected edit.
    reloaded = hyp_registry.get_hypothesis(hyp.id)
    assert reloaded.param_grid_json == {"momentum_window": [3, 6]}


# ── param_grid_json validation (FIX 2) ────────────────────────────────────────


def test_register_rejects_non_serializable_param_grid(hyp_registry: HypothesisRegistry) -> None:
    with pytest.raises(InvalidHypothesisError):
        hyp_registry.register_hypothesis(
            strategy_id="v1_base_momentum",
            hypothesis_text="bad grid",
            param_grid_json={"w": {1, 2, 3}},  # a set -- not JSON-serializable
        )


def test_register_rejects_list_shaped_param_grid(hyp_registry: HypothesisRegistry) -> None:
    with pytest.raises(InvalidHypothesisError):
        hyp_registry.register_hypothesis(
            strategy_id="v1_base_momentum",
            hypothesis_text="bad shape",
            param_grid_json=[1, 2, 3],  # type: ignore[arg-type]
        )


def test_register_accepts_valid_dict_param_grid(hyp_registry: HypothesisRegistry) -> None:
    hyp = hyp_registry.register_hypothesis(
        strategy_id="v1_base_momentum",
        hypothesis_text="good grid",
        param_grid_json={"momentum_window": [3, 6, 12]},
    )
    assert hyp.param_grid_json == {"momentum_window": [3, 6, 12]}


def test_update_param_grid_rejects_non_serializable(hyp_registry: HypothesisRegistry) -> None:
    hyp = hyp_registry.register_hypothesis(
        strategy_id="v1_base_momentum",
        hypothesis_text="Momentum window sensitivity",
        param_grid_json={"momentum_window": [3, 6]},
    )
    with pytest.raises(InvalidHypothesisError):
        hyp_registry.update_param_grid(hyp.id, {"w": {1, 2, 3}})
    # Grid must remain unchanged after the rejected edit (fails before write).
    reloaded = hyp_registry.get_hypothesis(hyp.id)
    assert reloaded.param_grid_json == {"momentum_window": [3, 6]}


def test_update_param_grid_rejects_list_shape(hyp_registry: HypothesisRegistry) -> None:
    hyp = hyp_registry.register_hypothesis(
        strategy_id="v1_base_momentum",
        hypothesis_text="Momentum window sensitivity",
        param_grid_json={"momentum_window": [3, 6]},
    )
    with pytest.raises(InvalidHypothesisError):
        hyp_registry.update_param_grid(hyp.id, [1, 2, 3])  # type: ignore[arg-type]


def test_update_param_grid_allows_none(hyp_registry: HypothesisRegistry) -> None:
    hyp = hyp_registry.register_hypothesis(
        strategy_id="v1_base_momentum",
        hypothesis_text="Momentum window sensitivity",
        param_grid_json={"momentum_window": [3, 6]},
    )
    updated = hyp_registry.update_param_grid(hyp.id, None)
    assert updated.param_grid_json is None


# ── CLI smoke test ──────────────────────────────────────────────────────────────


def test_cli_hypothesis_register_smoke(tmp_path: Path) -> None:
    """Invoke the `python -m strategy_registry hypothesis-register` command
    end-to-end and assert the row lands in the DB (acceptance evidence, §7
    row 04-3: "a CLI entry to invoke registration").
    """
    db_path = tmp_path / "cli_hypothesis.db"
    db_url = f"sqlite:///{db_path}"

    grid_path = tmp_path / "grid.json"
    grid_path.write_text(json.dumps({"momentum_window": [3, 6, 12]}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "strategy_registry",
            "hypothesis-register",
            "--strategy-id",
            "v1_base_momentum",
            "--text",
            "CLI smoke test hypothesis",
            "--param-grid-json",
            str(grid_path),
        ],
        env={**__import__("os").environ, "DATABASE_URL": db_url},
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "Hypothesis registered" in result.stdout

    reg = HypothesisRegistry(db_url)
    rows = reg.list_hypotheses("v1_base_momentum")
    assert len(rows) == 1
    assert rows[0].hypothesis_text == "CLI smoke test hypothesis"
    assert rows[0].param_grid_json == {"momentum_window": [3, 6, 12]}
    assert rows[0].frozen_at is None
