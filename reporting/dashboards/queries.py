"""All DB query helpers for the Streamlit dashboard.

No raw SQL in page files — pages import from this module only.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


# ---------------------------------------------------------------------------
# Blotter approval queries (Sprint 1 — Page 4)
# ---------------------------------------------------------------------------

def pending_blotter(artifact_dir: Path, engine: "Engine") -> dict | None:
    """Return the blotter artifact dict if one awaits approval, else None.

    A pending blotter is detected by scanning the shared artifact volume for
    blotter JSON files written in the last 36 hours whose run_id has no
    corresponding row in blotter_approvals (Section 5.3).
    """
    if not artifact_dir.is_dir():
        return None

    cutoff = datetime.utcnow() - timedelta(hours=36)
    candidates = sorted(
        artifact_dir.glob("**/blotter*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for path in candidates:
        if datetime.utcfromtimestamp(path.stat().st_mtime) < cutoff:
            break
        try:
            with open(path) as f:
                blotter = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        run_id = blotter.get("run_id")
        if not run_id:
            continue

        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM blotter_approvals WHERE blotter_run_id = :rid"),
                {"rid": run_id},
            ).fetchone()

        if not exists:
            return {"path": path, "blotter": blotter}

    return None


def insert_blotter_approval(
    engine: "Engine",
    *,
    run_id: str,
    local_path: str,
    blotter_sha256: str,
    selected_ids: list[str],
    approved_by: str,
    confirmed_hash: str,
    session_id: str,
    quantity_overrides: dict[str, int] | None,
) -> None:
    """INSERT an approval row into blotter_approvals (append-only, C3).

    The UNIQUE constraint on blotter_run_id prevents double-approval.
    Callers should catch IntegrityError to handle the race condition.
    """
    is_pg = engine.dialect.name == "postgresql"
    if is_pg:
        sql = text("""
            INSERT INTO blotter_approvals (
                blotter_run_id, blotter_local_path, blotter_sha256,
                selected_order_ids, approved_by, confirmed_blotter_sha256,
                dashboard_session_id, quantity_overrides, notes
            ) VALUES (
                :run_id, :local_path, :blotter_sha256,
                :selected_ids::jsonb, :approved_by, :confirmed_hash,
                :session_id, :overrides::jsonb, NULL
            )
        """)
    else:
        sql = text("""
            INSERT INTO blotter_approvals (
                blotter_run_id, blotter_local_path, blotter_sha256,
                selected_order_ids, approved_by, confirmed_blotter_sha256,
                dashboard_session_id, quantity_overrides, notes
            ) VALUES (
                :run_id, :local_path, :blotter_sha256,
                :selected_ids, :approved_by, :confirmed_hash,
                :session_id, :overrides, NULL
            )
        """)

    with engine.begin() as conn:
        conn.execute(
            sql,
            {
                "run_id": run_id,
                "local_path": local_path,
                "blotter_sha256": blotter_sha256,
                "selected_ids": json.dumps(selected_ids),
                "approved_by": approved_by,
                "confirmed_hash": confirmed_hash,
                "session_id": session_id,
                "overrides": json.dumps(quantity_overrides or {}),
            },
        )


def blotter_approval_history(engine: "Engine", limit: int = 50) -> pd.DataFrame:
    """Return recent blotter approval rows, newest first."""
    with engine.connect() as conn:
        df = pd.read_sql_query(
            text("""
                SELECT blotter_run_id, approved_at_utc, approved_by,
                       selected_order_ids, confirmed_blotter_sha256,
                       dashboard_session_id, notes
                FROM blotter_approvals
                ORDER BY approved_at_utc DESC
                LIMIT :lim
            """),
            conn,
            params={"lim": limit},
        )
    return df


# ---------------------------------------------------------------------------
# Overview queries (Sprint 2 stubs)
# ---------------------------------------------------------------------------

def latest_portfolio_snapshot(
    engine: "Engine", strategy_id: str
) -> dict | None:
    """Return the latest portfolio snapshot row, or None."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT snapshot_date, strategy_id, cash_usd, positions,
                       nav_usd, source, fetched_at_utc
                FROM portfolio_snapshots
                WHERE strategy_id = :sid
                ORDER BY snapshot_date DESC
                LIMIT 1
            """),
            {"sid": strategy_id},
        ).mappings().fetchone()
    if row is None:
        return None
    return dict(row)


def nav_history(
    engine: "Engine", strategy_id: str, lookback_days: int = 365
) -> pd.DataFrame:
    cutoff = date.today() - timedelta(days=lookback_days)
    with engine.connect() as conn:
        return pd.read_sql_query(
            text("""
                SELECT snapshot_date, nav_usd
                FROM portfolio_snapshots
                WHERE strategy_id = :sid AND snapshot_date >= :cutoff
                ORDER BY snapshot_date ASC
            """),
            conn,
            params={"sid": strategy_id, "cutoff": cutoff},
        )


def pipeline_health(engine: "Engine") -> dict:
    """Infer pipeline health from data recency (D3 decision)."""
    with engine.connect() as conn:
        prices_age = conn.execute(
            text("SELECT NOW() - MAX(ingested_at) FROM daily_prices")
        ).scalar()
        scores_age = conn.execute(
            text("SELECT NOW() - MAX(computed_at) FROM alpha_scores")
        ).scalar()
        sim_age = conn.execute(
            text("SELECT NOW() - MAX(computed_at_utc) FROM strategy_simulations")
        ).scalar()

    return {
        "prices": {"age": prices_age, "ok": prices_age is not None and prices_age < timedelta(hours=28)},
        "signals": {"age": scores_age, "ok": scores_age is not None and scores_age < timedelta(hours=6)},
        "simulations": {"age": sim_age, "ok": sim_age is not None and sim_age < timedelta(hours=6)},
    }


# ---------------------------------------------------------------------------
# Signal queries (Sprint 3 stubs)
# ---------------------------------------------------------------------------

def latest_alpha_scores(
    engine: "Engine", strategy_id: str, limit: int = 50
) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql_query(
            text("""
                SELECT ticker, alpha_score, rank, universe_size
                FROM alpha_scores
                WHERE score_date = (
                    SELECT MAX(score_date) FROM alpha_scores
                    WHERE strategy_id = :sid
                )
                AND strategy_id = :sid
                ORDER BY rank ASC
                LIMIT :lim
            """),
            conn,
            params={"sid": strategy_id, "lim": limit},
        )


def factor_scores_for_ticker(
    engine: "Engine", ticker: str, strategy_id: str, lookback_days: int = 30
) -> pd.DataFrame:
    cutoff = date.today() - timedelta(days=lookback_days)
    with engine.connect() as conn:
        return pd.read_sql_query(
            text("""
                SELECT score_date, factor_name, z_score, raw_value
                FROM factor_scores
                WHERE ticker = :ticker AND strategy_id = :sid
                  AND score_date >= :cutoff
                ORDER BY score_date ASC, factor_name ASC
            """),
            conn,
            params={"ticker": ticker, "sid": strategy_id, "cutoff": cutoff},
        )


# ---------------------------------------------------------------------------
# Fill / audit queries (Sprint 4 stubs)
# ---------------------------------------------------------------------------

def fill_history(
    engine: "Engine",
    strategy_id: str | None = None,
    ticker: str | None = None,
    start: date | None = None,
    end: date | None = None,
    side: str | None = None,
    limit: int = 1000,
) -> pd.DataFrame:
    conditions = ["1=1"]
    params: dict = {"lim": limit}
    if strategy_id:
        conditions.append("strategy_id = :sid")
        params["sid"] = strategy_id
    if ticker:
        conditions.append("ticker = :ticker")
        params["ticker"] = ticker
    if start:
        conditions.append("fill_timestamp >= :start")
        params["start"] = start
    if end:
        conditions.append("fill_timestamp <= :end")
        params["end"] = end
    if side:
        conditions.append("side = :side")
        params["side"] = side

    where = " AND ".join(conditions)
    with engine.connect() as conn:
        return pd.read_sql_query(
            text(f"""
                SELECT fill_id, fill_timestamp, ticker, strategy_id, side,
                       filled_quantity, avg_fill_price, realized_pnl,
                       cost_basis_per_share, wash_sale_disallowed, notes
                FROM trade_fills
                WHERE {where}
                ORDER BY fill_timestamp DESC
                LIMIT :lim
            """),
            conn,
            params=params,
        )


def realized_pnl_summary(
    engine: "Engine", strategy_id: str, start: date | None = None
) -> pd.DataFrame:
    conditions = ["side = 'SELL'", "realized_pnl IS NOT NULL", "strategy_id = :sid"]
    params: dict = {"sid": strategy_id}
    if start:
        conditions.append("fill_timestamp >= :start")
        params["start"] = start

    where = " AND ".join(conditions)
    with engine.connect() as conn:
        return pd.read_sql_query(
            text(f"""
                SELECT ticker, SUM(realized_pnl) AS total_pnl,
                       COUNT(*) AS n_fills,
                       bool_or(wash_sale_disallowed) AS has_wash_sale
                FROM trade_fills
                WHERE {where}
                GROUP BY ticker
                ORDER BY total_pnl DESC
            """),
            conn,
            params=params,
        )


def strategy_simulations_query(
    engine: "Engine",
    strategy_ids: list[str],
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    conditions = ["strategy_id = ANY(:sids)"]
    params: dict = {"sids": strategy_ids}
    if start:
        conditions.append("sim_date >= :start")
        params["start"] = start
    if end:
        conditions.append("sim_date <= :end")
        params["end"] = end

    where = " AND ".join(conditions)
    with engine.connect() as conn:
        return pd.read_sql_query(
            text(f"""
                SELECT strategy_id, sim_date, simulated_return,
                       simulated_nav, n_positions
                FROM strategy_simulations
                WHERE {where}
                ORDER BY sim_date ASC
            """),
            conn,
            params=params,
        )


def all_strategies(engine: "Engine") -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql_query(
            text("""
                SELECT strategy_id, status, created_at
                FROM strategies
                ORDER BY created_at DESC
            """),
            conn,
        )
