"""Tests for Sprint 2 query functions — portfolio snapshots, nav history, pipeline health."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from reporting.dashboards.queries import (
    active_strategy_id,
    latest_portfolio_snapshot,
    nav_history,
    previous_portfolio_snapshot,
    realized_pnl_summary,
)


@pytest.fixture
def engine():
    """Create an in-memory SQLite DB with Sprint 2 tables."""
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE portfolio_snapshots (
                id TEXT PRIMARY KEY,
                snapshot_date DATE NOT NULL,
                strategy_id TEXT NOT NULL,
                dag_run_id TEXT NOT NULL DEFAULT '',
                fetched_at_utc TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                cash_usd NUMERIC(18,6) NOT NULL,
                positions TEXT NOT NULL,
                nav_usd NUMERIC(18,6) NOT NULL,
                source TEXT NOT NULL DEFAULT 'ibkr_paper'
            )
        """))
        conn.execute(text("""
            CREATE TABLE strategies (
                strategy_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE trade_fills (
                fill_id TEXT PRIMARY KEY,
                fill_timestamp TIMESTAMP NOT NULL,
                ticker TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                side TEXT NOT NULL,
                filled_quantity NUMERIC NOT NULL,
                avg_fill_price NUMERIC NOT NULL,
                realized_pnl NUMERIC,
                cost_basis_per_share NUMERIC,
                wash_sale_disallowed BOOLEAN DEFAULT FALSE,
                notes TEXT
            )
        """))
    return eng


def _insert_snapshot(
    conn, snapshot_id: str, snap_date: str, strategy_id: str,
    cash: float, positions: list, nav: float
):
    conn.execute(
        text("""
            INSERT INTO portfolio_snapshots (id, snapshot_date, strategy_id, cash_usd, positions, nav_usd)
            VALUES (:id, :dt, :sid, :cash, :pos, :nav)
        """),
        {
            "id": snapshot_id,
            "dt": snap_date,
            "sid": strategy_id,
            "cash": cash,
            "pos": json.dumps(positions),
            "nav": nav,
        },
    )


class TestLatestPortfolioSnapshot:
    def test_returns_none_when_empty(self, engine):
        result = latest_portfolio_snapshot(engine, "v1")
        assert result is None

    def test_returns_latest(self, engine):
        with engine.begin() as conn:
            _insert_snapshot(conn, "s1", "2026-06-28", "v1", 500.0, [], 10500.0)
            _insert_snapshot(conn, "s2", "2026-06-29", "v1", 400.0, [{"ticker": "AAPL", "quantity": 10}], 10600.0)

        result = latest_portfolio_snapshot(engine, "v1")
        assert result is not None
        assert result["snapshot_date"] == "2026-06-29"
        assert float(result["nav_usd"]) == 10600.0

    def test_filters_by_strategy(self, engine):
        with engine.begin() as conn:
            _insert_snapshot(conn, "s1", "2026-06-29", "v1", 500.0, [], 10000.0)
            _insert_snapshot(conn, "s2", "2026-06-29", "v2", 300.0, [], 8000.0)

        result = latest_portfolio_snapshot(engine, "v2")
        assert result is not None
        assert float(result["nav_usd"]) == 8000.0


class TestPreviousPortfolioSnapshot:
    def test_returns_none_with_single_snapshot(self, engine):
        with engine.begin() as conn:
            _insert_snapshot(conn, "s1", "2026-06-29", "v1", 500.0, [], 10000.0)

        result = previous_portfolio_snapshot(engine, "v1")
        assert result is None

    def test_returns_second_latest(self, engine):
        with engine.begin() as conn:
            _insert_snapshot(conn, "s1", "2026-06-28", "v1", 600.0, [], 10000.0)
            _insert_snapshot(conn, "s2", "2026-06-29", "v1", 500.0, [], 10500.0)

        result = previous_portfolio_snapshot(engine, "v1")
        assert result is not None
        assert result["snapshot_date"] == "2026-06-28"


class TestNavHistory:
    def test_empty(self, engine):
        df = nav_history(engine, "v1", lookback_days=30)
        assert len(df) == 0

    def test_returns_ordered_data(self, engine):
        with engine.begin() as conn:
            for i in range(5):
                dt = f"2026-06-{25+i:02d}"
                _insert_snapshot(conn, f"s{i}", dt, "v1", 500.0, [], 10000.0 + i * 100)

        df = nav_history(engine, "v1", lookback_days=30)
        assert len(df) == 5
        assert float(df.iloc[0]["nav_usd"]) == 10000.0
        assert float(df.iloc[4]["nav_usd"]) == 10400.0

    def test_respects_lookback(self, engine):
        with engine.begin() as conn:
            _insert_snapshot(conn, "s1", "2025-01-01", "v1", 500.0, [], 9000.0)
            _insert_snapshot(conn, "s2", "2026-06-29", "v1", 500.0, [], 10000.0)

        df = nav_history(engine, "v1", lookback_days=30)
        assert len(df) == 1


class TestActiveStrategyId:
    def test_returns_paper_strategy(self, engine):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO strategies (strategy_id, status) VALUES ('v1', 'paper')"
            ))
            conn.execute(text(
                "INSERT INTO strategies (strategy_id, status) VALUES ('v2', 'backtesting')"
            ))

        result = active_strategy_id(engine)
        assert result == "v1"

    def test_returns_none_when_no_paper(self, engine):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO strategies (strategy_id, status) VALUES ('v1', 'backtesting')"
            ))

        result = active_strategy_id(engine)
        assert result is None


class TestRealizedPnlSummary:
    def test_empty(self, engine):
        df = realized_pnl_summary(engine, "v1")
        assert len(df) == 0

    def test_aggregates_sells(self, engine):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO trade_fills (fill_id, fill_timestamp, ticker, strategy_id, side,
                    filled_quantity, avg_fill_price, realized_pnl, wash_sale_disallowed)
                VALUES
                    ('f1', '2026-06-28 10:00:00', 'AAPL', 'v1', 'SELL', 10, 200, 150.0, 0),
                    ('f2', '2026-06-29 10:00:00', 'AAPL', 'v1', 'SELL', 5, 205, 75.0, 0),
                    ('f3', '2026-06-29 10:00:00', 'MSFT', 'v1', 'SELL', 3, 400, -20.0, 1),
                    ('f4', '2026-06-28 10:00:00', 'AAPL', 'v1', 'BUY', 15, 195, NULL, 0)
            """))

        df = realized_pnl_summary(engine, "v1")
        assert len(df) == 2

        aapl_row = df[df["ticker"] == "AAPL"].iloc[0]
        assert float(aapl_row["total_pnl"]) == 225.0
        assert int(aapl_row["n_fills"]) == 2

        msft_row = df[df["ticker"] == "MSFT"].iloc[0]
        assert float(msft_row["total_pnl"]) == -20.0
        assert int(msft_row["has_wash_sale"]) == 1
