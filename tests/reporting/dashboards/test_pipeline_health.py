"""Tests for reporting.dashboards.queries.pipeline_health (BUG-009 section 4
/ BUG-072, adversarial review round 6): the signals recency check must be
scoped to the active daily_signal_pipeline_operational research run, not
read across all runs -- a stale/superseded run leaving a fresher row behind
must not make the dashboard report the pipeline as healthy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from reporting.dashboards.queries import pipeline_health

ACTIVE_RESEARCH_RUN_ID = 1


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE daily_prices (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                date DATE NOT NULL,
                close NUMERIC NOT NULL,
                ingested_at TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE alpha_scores (
                id INTEGER PRIMARY KEY,
                score_date DATE NOT NULL,
                strategy_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                alpha_score NUMERIC NOT NULL,
                rank INTEGER NOT NULL,
                universe_size INTEGER NOT NULL,
                research_run_id INTEGER NOT NULL DEFAULT 1,
                computed_at TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE strategy_simulations (
                id INTEGER PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                sim_date DATE NOT NULL,
                simulated_return NUMERIC NOT NULL,
                simulated_nav NUMERIC NOT NULL,
                n_positions INTEGER NOT NULL,
                computed_at_utc TIMESTAMP
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
    return eng


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class TestPipelineHealthActiveRunFiltering:
    def test_signals_ok_when_active_run_is_fresh(self, engine) -> None:
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO alpha_scores "
                "(score_date, strategy_id, ticker, alpha_score, rank, universe_size, "
                " research_run_id, computed_at) "
                "VALUES ('2026-06-29', 'v1', 'AAPL', 1.0, 1, 10, 1, :ts)"
            ), {"ts": _iso(now - timedelta(hours=1))})

        health = pipeline_health(engine)
        assert health["signals"]["ok"] is True

    def test_stale_inactive_run_row_ignored_even_if_fresher(self, engine) -> None:
        """The exact scenario Codex flagged: a superseded/inactive run has a
        FRESHER row than the active run's stale/absent data. The dashboard
        must not report healthy based on the inactive run's freshness."""
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            # Active run's only row: stale (8 hours old, past the 6h threshold).
            conn.execute(text(
                "INSERT INTO alpha_scores "
                "(score_date, strategy_id, ticker, alpha_score, rank, universe_size, "
                " research_run_id, computed_at) "
                "VALUES ('2026-06-29', 'v1', 'AAPL', 1.0, 1, 10, 1, :ts)"
            ), {"ts": _iso(now - timedelta(hours=8))})

            # A second, INACTIVE run with a much fresher row.
            conn.execute(text(
                "INSERT INTO research_methodologies (id, name) VALUES (2, 'stale_methodology')"
            ))
            conn.execute(text(
                "INSERT INTO research_runs (id, methodology_id, is_active) VALUES (2, 2, 0)"
            ))
            conn.execute(text(
                "INSERT INTO alpha_scores "
                "(score_date, strategy_id, ticker, alpha_score, rank, universe_size, "
                " research_run_id, computed_at) "
                "VALUES ('2026-06-29', 'v1', 'ZZZZ', 99.0, 1, 10, 2, :ts)"
            ), {"ts": _iso(now - timedelta(minutes=1))})

        health = pipeline_health(engine)
        # Must reflect the ACTIVE run's stale row, not the inactive run's
        # fresh one -- signals must be reported unhealthy.
        assert health["signals"]["ok"] is False
        assert health["signals"]["age"] >= timedelta(hours=6)

    def test_no_active_run_reports_signals_unhealthy_not_crash(self, engine) -> None:
        with engine.begin() as conn:
            conn.execute(text("UPDATE research_runs SET is_active = 0"))
            conn.execute(text(
                "INSERT INTO alpha_scores "
                "(score_date, strategy_id, ticker, alpha_score, rank, universe_size, "
                " research_run_id, computed_at) "
                "VALUES ('2026-06-29', 'v1', 'AAPL', 1.0, 1, 10, 1, :ts)"
            ), {"ts": _iso(datetime.now(timezone.utc))})

        health = pipeline_health(engine)  # must not raise
        assert health["signals"]["age"] is None
        assert health["signals"]["ok"] is False

    def test_prices_and_simulations_unaffected_by_active_run_filter(self, engine) -> None:
        """Sanity: only the signals leg is scoped by research_run_id; prices
        and simulations recency are unrelated to research identity."""
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO daily_prices (ticker, date, close, ingested_at) "
                "VALUES ('AAPL', '2026-06-29', 200.0, :ts)"
            ), {"ts": _iso(now - timedelta(hours=1))})
            conn.execute(text(
                "INSERT INTO strategy_simulations "
                "(strategy_id, sim_date, simulated_return, simulated_nav, n_positions, computed_at_utc) "
                "VALUES ('v1', '2026-06-29', 0.01, 10100, 20, :ts)"
            ), {"ts": _iso(now - timedelta(hours=1))})

        health = pipeline_health(engine)
        assert health["prices"]["ok"] is True
        assert health["simulations"]["ok"] is True
