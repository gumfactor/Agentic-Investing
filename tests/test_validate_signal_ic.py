"""Tests for scripts/validate_signal_ic.py."""

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from scripts.validate_signal_ic import (
    _FACTORS,
    _add_gate_columns,
    _holdout_start,
    _persist_summary,
)


def test_default_factor_registry_covers_phase_two_factors():
    assert set(_FACTORS) == {"momentum", "lowvol", "value", "quality"}
    assert _FACTORS["value"].needs_fundamentals
    assert not _FACTORS["momentum"].needs_fundamentals


def test_holdout_start_uses_chronological_fraction():
    dates = [date(2024, 1, day) for day in range(1, 11)]
    assert _holdout_start(dates, 0.70) == date(2024, 1, 8)


def test_holdout_start_rejects_invalid_fraction():
    with pytest.raises(ValueError, match="between 0 and 1"):
        _holdout_start([date(2024, 1, 1), date(2024, 1, 2)], 1.0)


def test_gate_requires_ic_and_tstat():
    summary = pd.DataFrame(
        {
            "ic": [0.04, 0.02, 0.04],
            "ic_tstat": [2.5, 3.0, 1.5],
        }
    )
    result = _add_gate_columns(summary, min_ic=0.03, min_tstat=2.0)
    assert result["passes_gate"].tolist() == [True, False, False]


def test_persist_summary_uses_signal_ic_upsert():
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    summary = pd.DataFrame(
        [
            {
                "factor_name": "momentum",
                "strategy_id": "v1",
                "eval_date": date(2026, 5, 8),
                "horizon_days": 21,
                "ic": 0.09,
                "rank_ic": 0.02,
                "ic_tstat": 8.0,
                "ic_ir": 0.4,
                "ic_pvalue": 0.0,
                "n_observations": 356,
            }
        ]
    )

    assert _persist_summary(engine, summary) == 1
    statement, records = connection.execute.call_args.args
    sql = str(statement)
    assert "INSERT INTO signal_ic_stats" in sql
    assert "ON CONFLICT" in sql
    assert records[0]["factor_name"] == "momentum"
