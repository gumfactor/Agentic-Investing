"""Conformance tests for the fail-closed strategy-config contract.

Roadmap 02B / BUG-075. Two things this file guarantees:

1. Every key that currently appears in any ``config/strategy/*.yaml`` file
   is EXPLICITLY classified by ``backtesting/config_contract.py`` -- never
   silently allowed just because nobody wrote a check for it. Adding a new
   top-level section to an existing YAML with no corresponding entry in
   ``config_contract.py`` makes ``test_every_yaml_key_is_explicitly_classified``
   fail, because :func:`backtesting.config_contract.field_status` returns
   ``"unknown"`` for anything it was not taught about.

2. The two currently-shipped configs behave as designed: v1 (equal-weight,
   nothing the engine can't honor) passes validation unchanged; v2 (mvo,
   constraints, risk_model, live-only execution fields) is rejected with
   every one of its unsupported fields listed in a single error, never
   silently downgraded to equal-weight.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from backtesting.config_contract import (
    ExecutionConfigMismatchError,
    UnsupportedStrategyConfigError,
    assert_fill_simulator_matches_config,
    field_status,
    validate_backtest_config,
)

_STRATEGY_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "strategy"


def _load_all_strategy_configs() -> dict[str, dict]:
    configs = {}
    for path in sorted(_STRATEGY_CONFIG_DIR.glob("*.yaml")):
        with open(path, encoding="utf-8") as fh:
            configs[path.name] = yaml.safe_load(fh)
    return configs


def _flatten_dot_paths(config: dict, max_depth: int = 2) -> set[str]:
    """Return every dot-path in ``config`` up to ``max_depth`` segments.

    Depth is capped at 2 because that is all ``field_status``/
    ``validate_backtest_config`` ever inspect -- classification for a
    wildcard-informational section (``universe``, ``indicators``,
    ``reporting``) is the same regardless of how deeply it is nested, and
    every known section (``portfolio``, ``execution``, ``backtest``) is
    exactly one level deep in both shipped configs.

    Depth-cap invariant (02B round-2 P2-4): capping at 2 cannot hide an
    unclassified deeper key, because rejection happens at the PARENT level
    first. A depth-3 key can only exist under (a) a wildcard-informational
    section, where the entire subtree is informational by definition at
    any depth; (b) a rejected section, where the parent's presence already
    fails validation before any child matters; or (c) a known section's
    sub-key, where a dict-valued sub-key is either itself in the allowlist
    (reviewed as a unit) or rejected by name at depth 2. There is no path
    by which a depth-3 key reaches the engine without its depth-<=2
    ancestor having been explicitly classified, so flattening deeper would
    add assertions that can never fire.
    """
    paths: set[str] = set()
    for key, value in config.items():
        paths.add(key)
        if max_depth > 1 and isinstance(value, dict):
            for sub_key in value:
                paths.add(f"{key}.{sub_key}")
    return paths


_ALL_CONFIGS = _load_all_strategy_configs()


@pytest.mark.parametrize("filename", sorted(_ALL_CONFIGS))
def test_every_yaml_key_is_explicitly_classified(filename: str) -> None:
    """No key in any strategy YAML may be invisible to the contract.

    This is the CI trip-wire: if a new key is added to a strategy config
    (in an existing section or a brand new one) without also updating
    ``backtesting/config_contract.py`` to mark it CONSUMED, INFORMATIONAL,
    or an explicitly REJECTED section/key, this test fails instead of the
    key silently being allowed through unchecked.
    """
    config = _ALL_CONFIGS[filename]
    for dot_path in sorted(_flatten_dot_paths(config)):
        status = field_status(dot_path)
        assert status != "unknown", (
            f"{filename}: key '{dot_path}' is not classified by "
            "backtesting/config_contract.py (field_status returned "
            "'unknown'). Add it to the contract as CONSUMED, "
            "INFORMATIONAL, or an explicitly rejected section/key -- do "
            "not let a new key pass through unclassified (Roadmap 02B / "
            "BUG-075)."
        )


def test_v1_base_momentum_passes_validation() -> None:
    config = _ALL_CONFIGS["v1_base_momentum.yaml"]
    validate_backtest_config(config)  # must not raise


def test_v2_mvo_momentum_is_rejected_fail_closed() -> None:
    config = _ALL_CONFIGS["v2_mvo_momentum.yaml"]
    with pytest.raises(UnsupportedStrategyConfigError) as exc_info:
        validate_backtest_config(config)
    message = str(exc_info.value)
    expected_violations = [
        "portfolio.method=",
        "portfolio.optimizer_mode",
        "portfolio.drift_threshold",
        "'constraints' section",
        "'risk_model' section",
        "execution.broker",
        "execution.paper_trading",
        "execution.algo",
        "backtest.data_version",
    ]
    for fragment in expected_violations:
        assert fragment in message, (
            f"Expected violation fragment {fragment!r} missing from "
            f"UnsupportedStrategyConfigError message:\n{message}"
        )


@pytest.mark.parametrize(
    "portfolio_method,should_pass",
    [
        ("equal_weight", True),
        ("mvo", False),
        ("risk_parity", False),
        ("mean_variance", False),
    ],
)
def test_portfolio_method_value_restriction(
    portfolio_method: str, should_pass: bool
) -> None:
    config: dict[str, Any] = {
        "name": "unit_test_strategy",
        "data_version": "test",
        "portfolio": {"method": portfolio_method, "n_long": 10},
        "backtest": {
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "initial_capital": 100_000.0,
        },
    }
    if should_pass:
        validate_backtest_config(config)  # must not raise
    else:
        with pytest.raises(UnsupportedStrategyConfigError):
            validate_backtest_config(config)


@pytest.mark.parametrize(
    "section",
    ["constraints", "risk_model"],
)
def test_unsupported_sections_rejected_even_when_empty(section: str) -> None:
    config: dict[str, Any] = {
        "name": "unit_test_strategy",
        "data_version": "test",
        "portfolio": {"method": "equal_weight", "n_long": 10},
        "backtest": {
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "initial_capital": 100_000.0,
        },
        section: {},
    }
    with pytest.raises(UnsupportedStrategyConfigError):
        validate_backtest_config(config)


def test_unknown_top_level_key_rejected() -> None:
    config: dict[str, Any] = {
        "name": "unit_test_strategy",
        "data_version": "test",
        "portfolio": {"method": "equal_weight", "n_long": 10},
        "backtest": {
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "initial_capital": 100_000.0,
        },
        "totally_new_unreviewed_section": {"some_field": 1},
    }
    with pytest.raises(UnsupportedStrategyConfigError) as exc_info:
        validate_backtest_config(config)
    assert "totally_new_unreviewed_section" in str(exc_info.value)


def test_unknown_nested_key_in_known_section_rejected() -> None:
    config: dict[str, Any] = {
        "name": "unit_test_strategy",
        "data_version": "test",
        "portfolio": {
            "method": "equal_weight",
            "n_long": 10,
            "some_future_param_nobody_reviewed": True,
        },
        "backtest": {
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "initial_capital": 100_000.0,
        },
    }
    with pytest.raises(UnsupportedStrategyConfigError) as exc_info:
        validate_backtest_config(config)
    assert "portfolio.some_future_param_nobody_reviewed" in str(exc_info.value)


def test_backtest_data_version_nested_is_rejected_not_silently_ignored() -> None:
    """Guards the exact silent-ignore bug found in v2: a config that puts
    data_version under `backtest:` instead of top-level is never read by
    BacktestEngine.run and would leave BacktestResult.data_version empty.
    """
    config: dict[str, Any] = {
        "name": "unit_test_strategy",
        "portfolio": {"method": "equal_weight", "n_long": 10},
        "backtest": {
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "initial_capital": 100_000.0,
            "data_version": "rqis-snapshots/manifests/2026-06-14/manifest.json",
        },
    }
    with pytest.raises(UnsupportedStrategyConfigError) as exc_info:
        validate_backtest_config(config)
    assert "backtest.data_version" in str(exc_info.value)


def test_engine_run_rejects_unsupported_config_before_touching_data() -> None:
    """BacktestEngine.run must validate before doing any engine work."""
    from backtesting.engine.event_loop import BacktestEngine

    config: dict[str, Any] = {
        "name": "unit_test_strategy",
        "data_version": "test",
        "portfolio": {"method": "mvo", "n_long": 10},
        "backtest": {
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "initial_capital": 100_000.0,
        },
    }
    with pytest.raises(UnsupportedStrategyConfigError):
        # data_handler/fill_simulator are intentionally None/garbage --
        # validation must fail before either is ever touched.
        BacktestEngine().run(config, data_handler=None, fill_simulator=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 02B round-2 P0-1: declared execution params must match the actual simulator.
# ---------------------------------------------------------------------------


def _minimal_valid_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "name": "unit_test_strategy",
        "data_version": "test",
        "portfolio": {"method": "equal_weight", "n_long": 10},
        "backtest": {
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "initial_capital": 100_000.0,
        },
    }
    config.update(overrides)
    return config


def test_execution_mismatch_rejected_fail_closed() -> None:
    from backtesting.engine.fill_simulator import FillSimulator

    config = _minimal_valid_config(
        execution={
            "fill_model": "transaction_cost",
            "bid_ask_spread_bps": 10,
            "commission_per_share": 0.005,
        }
    )
    sim = FillSimulator(fill_model="perfect")  # actual != declared
    with pytest.raises(ExecutionConfigMismatchError) as exc_info:
        assert_fill_simulator_matches_config(config, sim)
    assert "execution.fill_model" in str(exc_info.value)


def test_execution_match_passes() -> None:
    from backtesting.engine.fill_simulator import FillSimulator

    config = _minimal_valid_config(
        execution={
            "fill_model": "transaction_cost",
            "bid_ask_spread_bps": 10,
            "market_impact_coeff": 0.5,
            "commission_per_share": 0.005,
        }
    )
    sim = FillSimulator(
        bid_ask_spread_bps=10.0,
        market_impact_coeff=0.5,
        commission_per_share=0.005,
        fill_model="transaction_cost",
    )
    assert_fill_simulator_matches_config(config, sim)  # must not raise


def test_execution_absent_passes_vacuously() -> None:
    """A config with no execution section declares nothing about costs."""
    assert_fill_simulator_matches_config(_minimal_valid_config(), object())


def test_execution_unverifiable_simulator_rejected() -> None:
    """A simulator that does not expose introspection attrs cannot be
    trusted to match the declared params -- unverifiable is mismatched."""
    config = _minimal_valid_config(execution={"fill_model": "perfect"})
    with pytest.raises(ExecutionConfigMismatchError) as exc_info:
        assert_fill_simulator_matches_config(config, object())
    assert "cannot be verified" in str(exc_info.value)


def test_engine_run_rejects_execution_mismatch() -> None:
    """BacktestEngine.run must fail closed on a declared/actual cost
    mismatch before touching any data."""
    from backtesting.engine.event_loop import BacktestEngine
    from backtesting.engine.fill_simulator import FillSimulator

    config = _minimal_valid_config(execution={"fill_model": "transaction_cost"})
    with pytest.raises(ExecutionConfigMismatchError):
        BacktestEngine().run(
            config,
            data_handler=None,  # type: ignore[arg-type]
            fill_simulator=FillSimulator(fill_model="perfect"),
        )


# ---------------------------------------------------------------------------
# 02B round-2 P0-2: loader fails closed when zero scores match strategy_id.
# ---------------------------------------------------------------------------


class _StubSnapshots:
    """Minimal ParquetSnapshots stand-in for loader fail-closed tests."""

    def __init__(self, alpha_scores: "pd.DataFrame") -> None:
        self._alpha_scores = alpha_scores

    def load_snapshot(self, data_type: str, snap_date: Any) -> "pd.DataFrame":
        import pandas as pd

        if data_type == "alpha_scores":
            return self._alpha_scores
        if data_type == "corporate_actions":
            return pd.DataFrame(columns=["ticker", "ex_date", "action_type", "value"])
        if data_type == "daily_prices":
            return pd.DataFrame(
                {"ticker": ["AAPL"], "date": ["2023-01-03"], "close": [100.0]}
            )
        if data_type == "benchmark":
            return pd.DataFrame({"date": ["2023-01-03"], "close": [400.0]})
        raise FileNotFoundError(data_type)


def test_loader_raises_when_no_scores_match_strategy_id() -> None:
    """Zero post-filter alpha_scores rows must raise, never warn-and-continue
    into a silent cash-only backtest labeled with the strategy's name."""
    import pandas as pd

    from backtesting.loader import load_from_snapshot

    scores = pd.DataFrame(
        {
            "ticker": ["AAPL"],
            "score_date": ["2023-01-03"],
            "alpha_score": [1.0],
            "strategy_id": ["some_other_strategy"],
        }
    )
    config = _minimal_valid_config(strategy_id="v9_does_not_exist")
    with pytest.raises(ValueError) as exc_info:
        load_from_snapshot("2023-01-05", config, snapshots=_StubSnapshots(scores))
    msg = str(exc_info.value)
    assert "v9_does_not_exist" in msg
    assert "cash-only" in msg


def test_loader_strategy_id_falls_back_to_name() -> None:
    """`name` is CONSUMED: with no explicit strategy_id, the loader filters
    scores by config['name'] -- proven here by the name appearing as the
    resolved id in the fail-closed error."""
    import pandas as pd

    from backtesting.loader import load_from_snapshot

    scores = pd.DataFrame(
        {
            "ticker": ["AAPL"],
            "score_date": ["2023-01-03"],
            "alpha_score": [1.0],
            "strategy_id": ["stored_under_different_id"],
        }
    )
    config = _minimal_valid_config()  # name=unit_test_strategy, no strategy_id
    with pytest.raises(ValueError) as exc_info:
        load_from_snapshot("2023-01-05", config, snapshots=_StubSnapshots(scores))
    assert "unit_test_strategy" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 02B round-2 P2-3: bare known-section names get a distinct status.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("section", ["portfolio", "execution", "backtest"])
def test_bare_known_section_name_has_distinct_status(section: str) -> None:
    assert field_status(section) == "section"


def test_name_and_version_are_consumed() -> None:
    """P0-2/sweep: `name` (loader strategy_id fallback + MLflow tag) and
    `version` (MLflow strategy_version tag) are keyed reads -> CONSUMED."""
    assert field_status("name") == "consumed"
    assert field_status("version") == "consumed"
