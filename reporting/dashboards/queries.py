"""All DB query helpers for the Streamlit dashboard.

No raw SQL in page files — pages import from this module only.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from sqlalchemy import exc as sa_exc, text

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

    cutoff = datetime.now(timezone.utc)- timedelta(hours=36)
    candidates = sorted(
        artifact_dir.glob("**/blotter*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    # Phase 1: collect candidate run_ids from recent files
    parsed: list[tuple[Path, dict]] = []
    for path in candidates:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            break
        try:
            with open(path) as f:
                blotter = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        run_id = blotter.get("run_id")
        if not run_id:
            continue
        parsed.append((path, blotter))

    if not parsed:
        return None

    # Phase 2: batch check which run_ids are already approved
    run_ids = [b.get("run_id") for _, b in parsed]
    approved_ids: set[str] = set()
    with engine.connect() as conn:
        for rid in run_ids:
            row = conn.execute(
                text("SELECT 1 FROM blotter_approvals WHERE blotter_run_id = :rid"),
                {"rid": rid},
            ).fetchone()
            if row:
                approved_ids.add(rid)

    # Phase 3: return the most recent pending
    for path, blotter in parsed:
        if blotter["run_id"] not in approved_ids:
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
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        prices_ts = conn.execute(
            text("SELECT MAX(ingested_at) FROM daily_prices")
        ).scalar()
        scores_ts = conn.execute(
            text("SELECT MAX(computed_at) FROM alpha_scores")
        ).scalar()
        try:
            sim_ts = conn.execute(
                text("SELECT MAX(computed_at_utc) FROM strategy_simulations")
            ).scalar()
        except (sa_exc.OperationalError, sa_exc.ProgrammingError):
            sim_ts = None

    def _age(ts: datetime | None) -> timedelta | None:
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return now - ts

    prices_age = _age(prices_ts)
    scores_age = _age(scores_ts)
    sim_age = _age(sim_ts)

    return {
        "prices": {"age": prices_age, "ok": prices_age is not None and prices_age < timedelta(hours=28)},
        "signals": {"age": scores_age, "ok": scores_age is not None and scores_age < timedelta(hours=6)},
        "simulations": {"age": sim_age, "ok": sim_age is not None and sim_age < timedelta(hours=6)},
    }


def previous_portfolio_snapshot(
    engine: "Engine", strategy_id: str
) -> dict | None:
    """Return the second-latest portfolio snapshot row (yesterday), or None."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT snapshot_date, strategy_id, cash_usd, positions,
                       nav_usd, source, fetched_at_utc
                FROM portfolio_snapshots
                WHERE strategy_id = :sid
                ORDER BY snapshot_date DESC
                LIMIT 1 OFFSET 1
            """),
            {"sid": strategy_id},
        ).mappings().fetchone()
    if row is None:
        return None
    return dict(row)


def latest_prices(engine: "Engine", tickers: list[str]) -> pd.DataFrame:
    """Return latest close prices for given tickers from daily_prices."""
    if not tickers:
        return pd.DataFrame(columns=["ticker", "close", "price_date"])
    placeholders = ", ".join(f":t_{i}" for i in range(len(tickers)))
    params = {f"t_{i}": t for i, t in enumerate(tickers)}
    with engine.connect() as conn:
        return pd.read_sql_query(
            text(f"""
                SELECT dp.ticker, dp.close, dp.date AS price_date
                FROM daily_prices dp
                INNER JOIN (
                    SELECT ticker, MAX(date) AS max_date
                    FROM daily_prices
                    WHERE ticker IN ({placeholders})
                    GROUP BY ticker
                ) latest ON dp.ticker = latest.ticker AND dp.date = latest.max_date
                ORDER BY dp.ticker
            """),
            conn,
            params=params,
        )


def daily_returns_for_tickers(
    engine: "Engine", tickers: list[str], lookback_days: int = 252
) -> pd.DataFrame:
    """Return daily returns (close-to-close) for risk computation."""
    if not tickers:
        return pd.DataFrame()
    cutoff = date.today() - timedelta(days=lookback_days + 30)
    placeholders = ", ".join(f":t_{i}" for i in range(len(tickers)))
    params = {f"t_{i}": t for i, t in enumerate(tickers)}
    params["cutoff"] = cutoff
    with engine.connect() as conn:
        df = pd.read_sql_query(
            text(f"""
                SELECT ticker, date AS price_date, close
                FROM daily_prices
                WHERE ticker IN ({placeholders})
                  AND date >= :cutoff
                ORDER BY ticker, date ASC
            """),
            conn,
            params=params,
        )
    if df.empty:
        return pd.DataFrame()
    pivot = df.pivot(index="price_date", columns="ticker", values="close")
    return pivot.pct_change().dropna(how="all")


def active_strategy_id(engine: "Engine") -> str | None:
    """Return the strategy_id with status='paper' (the active paper strategy)."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT strategy_id FROM strategies WHERE status = 'paper' LIMIT 1")
        ).fetchone()
    return row[0] if row else None


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


def bottom_alpha_scores(
    engine: "Engine", strategy_id: str, limit: int = 25
) -> pd.DataFrame:
    """Return bottom-ranked alpha scores for the latest score_date."""
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
                ORDER BY rank DESC
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
        conditions.append("fill_timestamp < :end_exclusive")
        params["end_exclusive"] = end + timedelta(days=1)
    if side:
        if side.upper() not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side: {side!r} — must be 'BUY' or 'SELL'")
        conditions.append("side = :side")
        params["side"] = side.upper()

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
                       MAX(CASE WHEN wash_sale_disallowed THEN 1 ELSE 0 END) AS has_wash_sale
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
    if not strategy_ids:
        return pd.DataFrame()
    placeholders = ", ".join(f":sid_{i}" for i in range(len(strategy_ids)))
    conditions = [f"strategy_id IN ({placeholders})"]
    params: dict = {f"sid_{i}": sid for i, sid in enumerate(strategy_ids)}
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
                SELECT strategy_id, status, registered_at AS created_at
                FROM strategies
                ORDER BY registered_at DESC
            """),
            conn,
        )


# ---------------------------------------------------------------------------
# Audit trail queries (Sprint 4 — Page 7 drill-down)
# ---------------------------------------------------------------------------

def alpha_score_at_fill_date(
    engine: "Engine", ticker: str, strategy_id: str, fill_date: date | str
) -> dict | None:
    """Return the alpha score for a ticker on or before a fill date."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT alpha_score, rank, universe_size, score_date
                FROM alpha_scores
                WHERE ticker = :ticker AND strategy_id = :sid
                  AND score_date <= :fdate
                ORDER BY score_date DESC
                LIMIT 1
            """),
            {"ticker": ticker, "sid": strategy_id, "fdate": fill_date},
        ).mappings().fetchone()
    return dict(row) if row else None


def factor_scores_at_fill_date(
    engine: "Engine", ticker: str, strategy_id: str, fill_date: date | str
) -> pd.DataFrame:
    """Return factor scores for a ticker on the latest score_date <= fill_date."""
    with engine.connect() as conn:
        return pd.read_sql_query(
            text("""
                SELECT factor_name, z_score, raw_value, score_date
                FROM factor_scores
                WHERE ticker = :ticker AND strategy_id = :sid
                  AND score_date <= :fdate
                  AND score_date = (
                      SELECT MAX(score_date) FROM factor_scores
                      WHERE ticker = :ticker AND strategy_id = :sid
                        AND score_date <= :fdate
                  )
                ORDER BY factor_name ASC
            """),
            conn,
            params={"ticker": ticker, "sid": strategy_id, "fdate": fill_date},
        )


def wash_sale_history(engine: "Engine") -> pd.DataFrame:
    """Return sell fills that are wash-sale-flagged or have negative P&L."""
    with engine.connect() as conn:
        return pd.read_sql_query(
            text("""
                SELECT fill_id, fill_timestamp, ticker, filled_quantity,
                       avg_fill_price, realized_pnl, wash_sale_disallowed
                FROM trade_fills
                WHERE side = 'SELL'
                  AND (wash_sale_disallowed = TRUE OR realized_pnl < 0)
                ORDER BY fill_timestamp DESC
            """),
            conn,
        )
