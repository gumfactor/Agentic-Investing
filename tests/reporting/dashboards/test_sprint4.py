"""Tests for Sprint 4 — simulation helpers, alpha overlap, audit trail queries."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from reporting.dashboards.queries import (
    blotter_approval_history,
    fill_history,
)
from reporting.dashboards.simulation import (
    alpha_overlap_matrix,
    build_simulated_nav_series,
    compute_simulated_return,
    jaccard_similarity,
)


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------


class TestComputeSimulatedReturn:
    def test_basic_return(self):
        weights = {"AAPL": 0.5, "MSFT": 0.5}
        prices_today = {"AAPL": 105.0, "MSFT": 210.0}
        prices_yesterday = {"AAPL": 100.0, "MSFT": 200.0}
        ret = compute_simulated_return(weights, prices_today, prices_yesterday)
        expected = 0.5 * 0.05 + 0.5 * 0.05
        assert abs(ret - expected) < 1e-9

    def test_missing_price_excluded(self):
        weights = {"AAPL": 0.5, "MSFT": 0.5}
        prices_today = {"AAPL": 105.0}
        prices_yesterday = {"AAPL": 100.0}
        ret = compute_simulated_return(weights, prices_today, prices_yesterday)
        assert abs(ret - 0.025) < 1e-9

    def test_empty_prices(self):
        weights = {"AAPL": 1.0}
        ret = compute_simulated_return(weights, {}, {})
        assert ret == 0.0

    def test_zero_yesterday_price(self):
        weights = {"AAPL": 1.0}
        ret = compute_simulated_return(
            weights, {"AAPL": 100.0}, {"AAPL": 0.0}
        )
        assert ret == 0.0


class TestBuildSimulatedNavSeries:
    def test_from_nav_column(self):
        df = pd.DataFrame({
            "sim_date": [date(2026, 1, 1), date(2026, 1, 2)],
            "simulated_nav": [10000.0, 10050.0],
            "simulated_return": [0.0, 0.005],
        })
        nav = build_simulated_nav_series(df)
        assert len(nav) == 2
        assert float(nav.iloc[-1]) == 10050.0

    def test_from_returns(self):
        df = pd.DataFrame({
            "sim_date": [date(2026, 1, 1), date(2026, 1, 2)],
            "simulated_return": [0.01, 0.02],
            "simulated_nav": [None, None],
        })
        nav = build_simulated_nav_series(df, initial_nav=10000.0)
        assert len(nav) == 2
        assert abs(float(nav.iloc[-1]) - 10000.0 * 1.01 * 1.02) < 0.01

    def test_empty(self):
        nav = build_simulated_nav_series(pd.DataFrame())
        assert len(nav) == 0


class TestJaccardSimilarity:
    def test_identical_sets(self):
        assert jaccard_similarity({"A", "B"}, {"A", "B"}) == 1.0

    def test_disjoint_sets(self):
        assert jaccard_similarity({"A"}, {"B"}) == 0.0

    def test_partial_overlap(self):
        sim = jaccard_similarity({"A", "B", "C"}, {"B", "C", "D"})
        assert abs(sim - 0.5) < 1e-9

    def test_empty_sets(self):
        assert jaccard_similarity(set(), set()) == 0.0


# ---------------------------------------------------------------------------
# Alpha overlap matrix (uses real DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
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
                universe_size INTEGER NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE trade_fills (
                fill_id TEXT PRIMARY KEY,
                fill_timestamp TIMESTAMP NOT NULL,
                ticker TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                side TEXT NOT NULL,
                filled_quantity INTEGER NOT NULL,
                avg_fill_price NUMERIC NOT NULL,
                realized_pnl NUMERIC,
                cost_basis_per_share NUMERIC,
                wash_sale_disallowed INTEGER DEFAULT 0,
                notes TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE blotter_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blotter_run_id TEXT NOT NULL UNIQUE,
                blotter_local_path TEXT NOT NULL,
                blotter_sha256 TEXT NOT NULL,
                selected_order_ids TEXT NOT NULL,
                approved_by TEXT NOT NULL,
                confirmed_blotter_sha256 TEXT NOT NULL,
                dashboard_session_id TEXT NOT NULL,
                approved_at_utc TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                quantity_overrides TEXT,
                notes TEXT
            )
        """))
    return eng


class TestAlphaOverlapMatrix:
    def test_overlap_with_data(self, engine):
        with engine.begin() as conn:
            for i, ticker in enumerate(["AAPL", "MSFT", "GOOG", "AMZN"]):
                conn.execute(
                    text("""
                        INSERT INTO alpha_scores (score_date, strategy_id, ticker, alpha_score, rank, universe_size)
                        VALUES ('2026-06-29', 'v1', :t, :s, :r, 50)
                    """),
                    {"t": ticker, "s": 1.0 - i * 0.1, "r": i + 1},
                )
            for i, ticker in enumerate(["AAPL", "MSFT", "TSLA", "META"]):
                conn.execute(
                    text("""
                        INSERT INTO alpha_scores (score_date, strategy_id, ticker, alpha_score, rank, universe_size)
                        VALUES ('2026-06-29', 'v2', :t, :s, :r, 50)
                    """),
                    {"t": ticker, "s": 1.0 - i * 0.1, "r": i + 1},
                )

        matrix = alpha_overlap_matrix(engine, ["v1", "v2"], top_n=4)
        assert matrix.shape == (2, 2)
        assert matrix.loc["v1", "v1"] == 1.0
        assert matrix.loc["v2", "v2"] == 1.0
        # 2 common (AAPL, MSFT) out of 6 unique = 2/6 = 0.333...
        assert abs(matrix.loc["v1", "v2"] - 1 / 3) < 0.01

    def test_no_data(self, engine):
        matrix = alpha_overlap_matrix(engine, ["v1", "v2"], top_n=10)
        assert matrix.shape == (2, 2)
        assert matrix.loc["v1", "v2"] == 0.0


# ---------------------------------------------------------------------------
# Fill history queries
# ---------------------------------------------------------------------------


class TestFillHistory:
    def test_empty(self, engine):
        df = fill_history(engine)
        assert len(df) == 0

    def test_returns_fills(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO trade_fills (fill_id, fill_timestamp, ticker, strategy_id,
                    side, filled_quantity, avg_fill_price, realized_pnl)
                VALUES
                    ('f1', '2026-06-29 10:00:00', 'AAPL', 'v1', 'BUY', 10, 190.50, NULL),
                    ('f2', '2026-06-29 10:01:00', 'MSFT', 'v1', 'SELL', 5, 410.25, 50.00)
            """))

        df = fill_history(engine)
        assert len(df) == 2

    def test_filters_by_side(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO trade_fills (fill_id, fill_timestamp, ticker, strategy_id,
                    side, filled_quantity, avg_fill_price)
                VALUES
                    ('f1', '2026-06-29 10:00:00', 'AAPL', 'v1', 'BUY', 10, 190.50),
                    ('f2', '2026-06-29 10:01:00', 'MSFT', 'v1', 'SELL', 5, 410.25)
            """))

        df = fill_history(engine, side="SELL")
        assert len(df) == 1
        assert df.iloc[0]["side"] == "SELL"

    def test_invalid_side_raises(self, engine):
        with pytest.raises(ValueError, match="Invalid side"):
            fill_history(engine, side="INVALID")

    def test_filters_by_ticker(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO trade_fills (fill_id, fill_timestamp, ticker, strategy_id,
                    side, filled_quantity, avg_fill_price)
                VALUES
                    ('f1', '2026-06-29 10:00:00', 'AAPL', 'v1', 'BUY', 10, 190.50),
                    ('f2', '2026-06-29 10:01:00', 'MSFT', 'v1', 'BUY', 5, 410.25)
            """))

        df = fill_history(engine, ticker="AAPL")
        assert len(df) == 1
        assert df.iloc[0]["ticker"] == "AAPL"


class TestBlotterApprovalHistory:
    def test_empty(self, engine):
        df = blotter_approval_history(engine)
        assert len(df) == 0

    def test_returns_approvals(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO blotter_approvals (
                    blotter_run_id, blotter_local_path, blotter_sha256,
                    selected_order_ids, approved_by, confirmed_blotter_sha256,
                    dashboard_session_id
                ) VALUES (
                    'run_001', '/path/to/blotter.json', 'abc123',
                    '["1","2"]', 'op@test.com', 'abc123', 'sess_001'
                )
            """))

        df = blotter_approval_history(engine)
        assert len(df) == 1
