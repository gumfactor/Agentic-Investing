"""Tests for Sprint 3 query functions — strategy simulations, alpha scores, factors."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, text

from reporting.dashboards.queries import (
    all_strategies,
    bottom_alpha_scores,
    factor_scores_for_ticker,
    latest_alpha_scores,
    strategy_simulations_query,
)

# BUG-009 section 4 / BUG-072: latest_alpha_scores/factor_scores_for_ticker
# now filter by the active daily_signal_pipeline_operational research run —
# every alpha_scores/factor_scores row inserted by these tests must carry
# ACTIVE_RESEARCH_RUN_ID, and the fixture DB needs the research_runs/
# research_methodologies tables the query's subquery joins against.
ACTIVE_RESEARCH_RUN_ID = 1


@pytest.fixture
def engine():
    """Create an in-memory SQLite DB with Sprint 3 tables."""
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE alpha_scores (
                id INTEGER PRIMARY KEY,
                score_date DATE NOT NULL,
                strategy_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                alpha_score NUMERIC NOT NULL,
                rank INTEGER NOT NULL,
                universe_size INTEGER NOT NULL,
                research_run_id INTEGER NOT NULL DEFAULT 1
            )
        """))
        conn.execute(text("""
            CREATE TABLE factor_scores (
                id INTEGER PRIMARY KEY,
                score_date DATE NOT NULL,
                strategy_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                factor_name TEXT NOT NULL,
                z_score NUMERIC NOT NULL,
                raw_value NUMERIC NOT NULL,
                research_run_id INTEGER NOT NULL DEFAULT 1
            )
        """))
        conn.execute(text("""
            CREATE TABLE strategy_simulations (
                id INTEGER PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                sim_date DATE NOT NULL,
                simulated_return NUMERIC NOT NULL,
                simulated_nav NUMERIC NOT NULL,
                n_positions INTEGER NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE strategies (
                strategy_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                registered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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


class TestLatestAlphaScores:
    def test_empty(self, engine):
        df = latest_alpha_scores(engine, "v1")
        assert len(df) == 0

    def test_returns_latest_date(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO alpha_scores (score_date, strategy_id, ticker, alpha_score, rank, universe_size)
                VALUES
                    ('2026-06-28', 'v1', 'AAPL', 0.85, 1, 50),
                    ('2026-06-28', 'v1', 'MSFT', 0.72, 2, 50),
                    ('2026-06-27', 'v1', 'GOOG', 0.90, 1, 50)
            """))

        df = latest_alpha_scores(engine, "v1")
        assert len(df) == 2
        assert set(df["ticker"].tolist()) == {"AAPL", "MSFT"}

    def test_respects_limit(self, engine):
        with engine.begin() as conn:
            for i in range(10):
                conn.execute(
                    text("""
                        INSERT INTO alpha_scores (score_date, strategy_id, ticker, alpha_score, rank, universe_size)
                        VALUES ('2026-06-29', 'v1', :t, :s, :r, 50)
                    """),
                    {"t": f"T{i}", "s": 1.0 - i * 0.1, "r": i + 1},
                )

        df = latest_alpha_scores(engine, "v1", limit=5)
        assert len(df) == 5


class TestActiveResearchRunFiltering:
    """BUG-009 section 4 / BUG-072 (adversarial review): latest_alpha_scores/
    bottom_alpha_scores/factor_scores_for_ticker must only ever return rows
    from the active daily_signal_pipeline_operational run, never a stale/
    superseded run's rows for the same natural key."""

    def test_latest_alpha_scores_excludes_inactive_run(self, engine):
        with engine.begin() as conn:
            # A second, INACTIVE methodology/run with a colliding row for
            # the same (ticker, score_date, strategy_id).
            conn.execute(text(
                "INSERT INTO research_methodologies (id, name) VALUES (2, 'stale_methodology')"
            ))
            conn.execute(text(
                "INSERT INTO research_runs (id, methodology_id, is_active) VALUES (2, 2, 0)"
            ))
            conn.execute(text("""
                INSERT INTO alpha_scores
                    (score_date, strategy_id, ticker, alpha_score, rank, universe_size, research_run_id)
                VALUES
                    ('2026-06-28', 'v1', 'AAPL', 0.85, 1, 50, 1),
                    ('2026-06-28', 'v1', 'ZZZZ', 99.0, 1, 50, 2)
            """))

        df = latest_alpha_scores(engine, "v1")
        assert set(df["ticker"].tolist()) == {"AAPL"}
        assert "ZZZZ" not in df["ticker"].tolist()

    def test_bottom_alpha_scores_excludes_inactive_run(self, engine):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO research_methodologies (id, name) VALUES (2, 'stale_methodology')"
            ))
            conn.execute(text(
                "INSERT INTO research_runs (id, methodology_id, is_active) VALUES (2, 2, 0)"
            ))
            conn.execute(text("""
                INSERT INTO alpha_scores
                    (score_date, strategy_id, ticker, alpha_score, rank, universe_size, research_run_id)
                VALUES
                    ('2026-06-28', 'v1', 'AAPL', 0.10, 5, 50, 1),
                    ('2026-06-28', 'v1', 'ZZZZ', -9.0, 5, 50, 2)
            """))

        df = bottom_alpha_scores(engine, "v1")
        assert set(df["ticker"].tolist()) == {"AAPL"}

    def test_factor_scores_for_ticker_excludes_inactive_run(self, engine):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO research_methodologies (id, name) VALUES (2, 'stale_methodology')"
            ))
            conn.execute(text(
                "INSERT INTO research_runs (id, methodology_id, is_active) VALUES (2, 2, 0)"
            ))
            conn.execute(text("""
                INSERT INTO factor_scores
                    (score_date, strategy_id, ticker, factor_name, z_score, raw_value, research_run_id)
                VALUES
                    ('2026-06-29', 'v1', 'AAPL', 'momentum', 1.5, 0.12, 1),
                    ('2026-06-29', 'v1', 'AAPL', 'stale_factor', 9.9, 9.9, 2)
            """))

        df = factor_scores_for_ticker(engine, "AAPL", "v1")
        assert list(df["factor_name"]) == ["momentum"]

    def test_no_active_run_returns_empty_not_crash(self, engine):
        with engine.begin() as conn:
            conn.execute(text("UPDATE research_runs SET is_active = 0"))
            conn.execute(text("""
                INSERT INTO alpha_scores
                    (score_date, strategy_id, ticker, alpha_score, rank, universe_size, research_run_id)
                VALUES ('2026-06-28', 'v1', 'AAPL', 0.85, 1, 50, 1)
            """))

        df = latest_alpha_scores(engine, "v1")
        assert df.empty


class TestFactorScoresForTicker:
    def test_empty(self, engine):
        df = factor_scores_for_ticker(engine, "AAPL", "v1")
        assert len(df) == 0

    def test_returns_factors(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO factor_scores (score_date, strategy_id, ticker, factor_name, z_score, raw_value)
                VALUES
                    ('2026-06-29', 'v1', 'AAPL', 'momentum', 1.5, 0.12),
                    ('2026-06-29', 'v1', 'AAPL', 'value', -0.3, 22.5),
                    ('2026-06-29', 'v1', 'MSFT', 'momentum', 0.8, 0.08)
            """))

        df = factor_scores_for_ticker(engine, "AAPL", "v1")
        assert len(df) == 2
        assert "momentum" in df["factor_name"].values
        assert "value" in df["factor_name"].values


class TestStrategySimulationsQuery:
    def test_empty(self, engine):
        df = strategy_simulations_query(engine, ["v1"])
        assert len(df) == 0

    def test_returns_data(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO strategy_simulations (strategy_id, sim_date, simulated_return, simulated_nav, n_positions)
                VALUES
                    ('v1', '2026-06-28', 0.005, 10050, 20),
                    ('v1', '2026-06-29', -0.002, 10030, 20),
                    ('v2', '2026-06-28', 0.008, 10080, 15),
                    ('v2', '2026-06-29', 0.003, 10110, 15)
            """))

        df = strategy_simulations_query(engine, ["v1", "v2"])
        assert len(df) == 4

    def test_filters_by_strategy(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO strategy_simulations (strategy_id, sim_date, simulated_return, simulated_nav, n_positions)
                VALUES
                    ('v1', '2026-06-29', 0.005, 10050, 20),
                    ('v2', '2026-06-29', 0.008, 10080, 15)
            """))

        df = strategy_simulations_query(engine, ["v1"])
        assert len(df) == 1
        assert df.iloc[0]["strategy_id"] == "v1"

    def test_date_range_filter(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO strategy_simulations (strategy_id, sim_date, simulated_return, simulated_nav, n_positions)
                VALUES
                    ('v1', '2026-01-01', 0.001, 10010, 20),
                    ('v1', '2026-06-29', 0.005, 10500, 20)
            """))

        df = strategy_simulations_query(engine, ["v1"], start=date(2026, 6, 1))
        assert len(df) == 1
        assert str(df.iloc[0]["sim_date"]) == "2026-06-29"


class TestAllStrategies:
    def test_empty(self, engine):
        df = all_strategies(engine)
        assert len(df) == 0

    def test_returns_all(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO strategies (strategy_id, status, registered_at) VALUES ('v1', 'paper', '2026-06-29 10:00:00')
            """))
            conn.execute(text("""
                INSERT INTO strategies (strategy_id, status, registered_at) VALUES ('v2', 'backtesting', '2026-06-28 10:00:00')
            """))

        df = all_strategies(engine)
        assert len(df) == 2
        assert df.iloc[0]["strategy_id"] == "v1"
        assert "created_at" in df.columns
