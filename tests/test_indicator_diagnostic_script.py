"""Tests for scripts/indicator_diagnostic.py::_load_factor_scores (BUG-072,
adversarial review round 8, closing the entry's last open item).

migration 012 widened factor_scores' PK/unique constraints to include
research_run_id, so more than one row can legitimately exist for the same
(ticker, score_date, strategy_id, factor_name) across research runs (legacy,
superseded, active). This tool's own duplicate-row detection
(backtesting/validation/indicator_diagnostic.py) only WARNS and then
pivot_table silently averages the duplicates -- a real correctness risk for
a tool whose entire purpose is measuring factor reliability/validity, not
just staleness. Default behavior must scope to the single active
daily_signal_pipeline_operational run; --all-runs is an explicit opt-in that
must fail closed (raise) rather than silently blend/average across runs.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from scripts.indicator_diagnostic import _load_factor_scores

ACTIVE_RESEARCH_RUN_ID = 1
INACTIVE_RESEARCH_RUN_ID = 2


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE factor_scores (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                score_date DATE NOT NULL,
                strategy_id TEXT NOT NULL,
                factor_name TEXT NOT NULL,
                z_score NUMERIC NOT NULL,
                research_run_id INTEGER NOT NULL DEFAULT 1
            )
        """))
        conn.execute(text("""
            CREATE TABLE research_methodologies (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE research_runs (
                id INTEGER PRIMARY KEY,
                methodology_id INTEGER NOT NULL,
                is_active BOOLEAN NOT NULL
            )
        """))
        conn.execute(text(
            "INSERT INTO research_methodologies (id, name) "
            "VALUES (1, 'daily_signal_pipeline_operational')"
        ))
        conn.execute(text(
            f"INSERT INTO research_runs (id, methodology_id, is_active) "
            f"VALUES ({ACTIVE_RESEARCH_RUN_ID}, 1, 1)"
        ))
        conn.execute(text(
            "INSERT INTO research_methodologies (id, name) VALUES (2, 'stale_methodology')"
        ))
        conn.execute(text(
            f"INSERT INTO research_runs (id, methodology_id, is_active) "
            f"VALUES ({INACTIVE_RESEARCH_RUN_ID}, 2, 0)"
        ))
    return eng


class TestLoadFactorScoresActiveRunFiltering:
    def test_default_scopes_to_active_run_only(self, engine) -> None:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO factor_scores "
                "(ticker, score_date, strategy_id, factor_name, z_score, research_run_id) "
                "VALUES ('AAPL', '2026-06-29', 'v1', 'momentum', 1.0, :run)"
            ), {"run": ACTIVE_RESEARCH_RUN_ID})
            # Same (ticker, score_date, strategy_id, factor_name) under the
            # inactive/superseded run -- must NOT be included by default.
            conn.execute(text(
                "INSERT INTO factor_scores "
                "(ticker, score_date, strategy_id, factor_name, z_score, research_run_id) "
                "VALUES ('AAPL', '2026-06-29', 'v1', 'momentum', 99.0, :run)"
            ), {"run": INACTIVE_RESEARCH_RUN_ID})

        df = _load_factor_scores(engine, "v1", None, None)
        assert len(df) == 1
        assert df.iloc[0]["z_score"] == 1.0

    def test_all_runs_opt_in_raises_on_duplicate_rows(self, engine) -> None:
        """--all-runs must fail closed, not silently average duplicates into
        the diagnostic pivot."""
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO factor_scores "
                "(ticker, score_date, strategy_id, factor_name, z_score, research_run_id) "
                "VALUES ('AAPL', '2026-06-29', 'v1', 'momentum', 1.0, :run)"
            ), {"run": ACTIVE_RESEARCH_RUN_ID})
            conn.execute(text(
                "INSERT INTO factor_scores "
                "(ticker, score_date, strategy_id, factor_name, z_score, research_run_id) "
                "VALUES ('AAPL', '2026-06-29', 'v1', 'momentum', 99.0, :run)"
            ), {"run": INACTIVE_RESEARCH_RUN_ID})

        with pytest.raises(ValueError, match="duplicate"):
            _load_factor_scores(engine, "v1", None, None, all_runs=True)

    def test_all_runs_opt_in_succeeds_when_no_overlap(self, engine) -> None:
        """--all-runs is a legitimate opt-in for genuine cross-run
        comparisons when the runs don't collide on the same natural key
        (e.g. disjoint score_date ranges)."""
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO factor_scores "
                "(ticker, score_date, strategy_id, factor_name, z_score, research_run_id) "
                "VALUES ('AAPL', '2026-06-29', 'v1', 'momentum', 1.0, :run)"
            ), {"run": ACTIVE_RESEARCH_RUN_ID})
            conn.execute(text(
                "INSERT INTO factor_scores "
                "(ticker, score_date, strategy_id, factor_name, z_score, research_run_id) "
                "VALUES ('AAPL', '2026-05-01', 'v1', 'momentum', 2.0, :run)"
            ), {"run": INACTIVE_RESEARCH_RUN_ID})

        df = _load_factor_scores(engine, "v1", None, None, all_runs=True)
        assert len(df) == 2

    def test_no_active_run_returns_empty_not_crash(self, engine) -> None:
        with engine.begin() as conn:
            conn.execute(text("UPDATE research_runs SET is_active = 0"))
            conn.execute(text(
                "INSERT INTO factor_scores "
                "(ticker, score_date, strategy_id, factor_name, z_score, research_run_id) "
                "VALUES ('AAPL', '2026-06-29', 'v1', 'momentum', 1.0, :run)"
            ), {"run": ACTIVE_RESEARCH_RUN_ID})

        df = _load_factor_scores(engine, "v1", None, None)
        assert df.empty
