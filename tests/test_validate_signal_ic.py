"""Tests for scripts/validate_signal_ic.py."""

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from scripts.validate_signal_ic import (
    _build_eligibility_frame,
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

    assert _persist_summary(engine, summary, research_run_id=42) == 1
    statement, records = connection.execute.call_args.args
    sql = str(statement)
    assert "INSERT INTO signal_ic_stats" in sql
    assert "ON CONFLICT" in sql
    assert "research_run_id" in sql
    assert records[0]["factor_name"] == "momentum"
    assert records[0]["research_run_id"] == 42
    # Default is provisional=True (BUG-008 interim marker, migration 010).
    assert records[0]["provisional"] is True
    assert "provisional = EXCLUDED.provisional" in sql


def test_persist_summary_requires_research_run_id():
    """BUG-009 section 4: a persisted row without a research_run_id would
    fall outside the run-scoped unique constraint added by migration 012."""
    engine = MagicMock()
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
    with pytest.raises(ValueError, match="research_run_id"):
        _persist_summary(engine, summary, research_run_id=None)


def test_persist_summary_stamps_certified_rows_non_provisional():
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

    # PIT-enforced run (universe lookup active) stamps provisional=False.
    assert _persist_summary(engine, summary, research_run_id=42, provisional=False) == 1
    _, records = connection.execute.call_args.args
    assert records[0]["provisional"] is False


def test_build_eligibility_frame_matches_lookup(tmp_path):
    """The pre-scoring eligibility frame must agree exactly with the PIT
    lookup used for IC merging (Codex PR #34 round-5 P1)."""
    from datetime import date

    from sqlalchemy import create_engine

    from data.universe.import_pipeline import run_import
    from data.universe.providers.fixture_provider import (
        FIXTURE_COVERAGE_START,
        FIXTURE_UNIVERSE_ID,
        FixtureSP500Provider,
    )
    from data.universe.runtime import PITUniverseLookup

    eng = create_engine(f"sqlite:///{tmp_path / 'u.db'}", future=True)
    run_import(
        FixtureSP500Provider(),
        engine=eng,
        artifact_root=tmp_path / "a",
        coverage_start=FIXTURE_COVERAGE_START,
    )
    lookup = PITUniverseLookup(eng, FIXTURE_UNIVERSE_ID)

    dates = [date(2021, 1, 1), date(2021, 6, 1), date(2022, 6, 1)]
    frame = _build_eligibility_frame(lookup, dates)
    for d in dates:
        expected = set(lookup.load_universe_as_of(d).eligible_tickers)
        actual = set(frame[frame["date"] == d]["ticker"])
        assert actual == expected


def test_eligibility_limited_to_scored_dates_avoids_coverage_gap(tmp_path):
    """Codex PR #34 P2: pre-holdout lookback price history may predate the
    published coverage window; eligibility is built only for scored
    (holdout) dates, so such runs succeed — while querying the full price
    date range would fail closed."""
    from datetime import date

    import pytest as _pytest
    from sqlalchemy import create_engine

    from data.universe.import_pipeline import run_import
    from data.universe.providers.fixture_provider import (
        FIXTURE_COVERAGE_START,
        FIXTURE_UNIVERSE_ID,
        FixtureSP500Provider,
    )
    from data.universe.runtime import CoverageGapError, PITUniverseLookup

    eng = create_engine(f"sqlite:///{tmp_path / 'u.db'}", future=True)
    run_import(
        FixtureSP500Provider(),
        engine=eng,
        artifact_root=tmp_path / "a",
        coverage_start=FIXTURE_COVERAGE_START,  # 2020-01-01
    )
    lookup = PITUniverseLookup(eng, FIXTURE_UNIVERSE_ID)

    # Price history reaches back before coverage; holdout starts inside it.
    all_price_dates = [date(2019, 6, 3), date(2019, 6, 4), date(2022, 6, 1), date(2022, 6, 2)]
    holdout_start = date(2022, 1, 1)
    scored_dates = [d for d in all_price_dates if d >= holdout_start]

    frame = _build_eligibility_frame(lookup, scored_dates)
    assert set(frame["date"]) == set(scored_dates)

    with _pytest.raises(CoverageGapError):
        _build_eligibility_frame(lookup, all_price_dates)
