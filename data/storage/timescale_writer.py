"""TimescaleDB writer for market data.

All writes are upserts (INSERT ... ON CONFLICT DO UPDATE) so ingestion
pipelines are idempotent — re-running a failed batch is always safe.

Prices are written as NUMERIC via psycopg2's Decimal adapter, which maps
Python Decimal directly to PostgreSQL NUMERIC without floating-point
intermediate representation. This preserves the precision guarantee that
the schema requires.

Connection management: TimescaleWriter holds a SQLAlchemy engine. Create one
instance per process / per Airflow task. Do not share across threads without
enabling pool_pre_ping.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import pandas as pd
import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = structlog.get_logger(__name__)

# Batch size for bulk inserts — balances memory and network round-trips.
_INSERT_BATCH_SIZE = 5000


class TimescaleWriter:
    """Writes market data to TimescaleDB via SQLAlchemy.

    Args:
        database_url: SQLAlchemy connection string. Defaults to DATABASE_URL env var.
        batch_size: Number of rows per INSERT batch.
    """

    def __init__(
        self,
        database_url: Optional[str] = None,
        batch_size: int = _INSERT_BATCH_SIZE,
    ) -> None:
        url = database_url or os.environ["DATABASE_URL"]
        self._engine: Engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=5,
        )
        self._batch_size = batch_size

    # ─── Public write methods ──────────────────────────────────────────────────

    def upsert_ohlcv(self, df: pd.DataFrame) -> int:
        """Upsert daily price bars into daily_prices.

        Returns the number of rows written (inserts + updates).
        """
        if df.empty:
            return 0

        required = {"ticker", "date", "close", "source"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"upsert_ohlcv: missing required columns {missing}")

        rows_written = 0

        for batch in _iter_batches(df, self._batch_size):
            rows = []
            for _, row in batch.iterrows():
                rows.append(
                    {
                        "ticker": row["ticker"],
                        "date": row["date"],
                        "open": _to_decimal_or_none(row.get("open")),
                        "high": _to_decimal_or_none(row.get("high")),
                        "low": _to_decimal_or_none(row.get("low")),
                        "close": Decimal(str(row["close"])),
                        "volume": _to_int_or_none(row.get("volume")),
                        "source_adj_close": _to_decimal_or_none(row.get("source_adj_close")),
                        "source": row["source"],
                    }
                )

            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO daily_prices
                            (ticker, date, open, high, low, close, volume, source_adj_close, source)
                        VALUES
                            (:ticker, :date, :open, :high, :low, :close, :volume, :source_adj_close, :source)
                        ON CONFLICT (ticker, date) DO UPDATE SET
                            open             = EXCLUDED.open,
                            high             = EXCLUDED.high,
                            low              = EXCLUDED.low,
                            close            = EXCLUDED.close,
                            volume           = EXCLUDED.volume,
                            source_adj_close = EXCLUDED.source_adj_close,
                            source           = EXCLUDED.source,
                            ingested_at      = NOW()
                        """
                    ),
                    rows,
                )
            rows_written += len(rows)

        logger.info("upsert_ohlcv_complete", rows_written=rows_written)
        return rows_written

    def upsert_corporate_actions(self, df: pd.DataFrame, source_version: str = "unknown") -> int:
        """Upsert corporate action records into corporate_actions.

        BUG-009 / 01B-3 (design plan §2.3): every row must carry a defensible
        availability timestamp (``known_at``) so downstream cutoff-aware
        adjustment builders (``data.normalization.corporate_actions``) can
        determine whether an action was knowable by a given score/return
        cutoff. ``announced_at`` may be supplied per-row via an optional
        ``announced_at`` column in *df*; when absent (the case for every
        current source — yfinance supplies ex-dates only), ``known_at`` is
        derived via the conservative date-only rule (no earlier than the
        close of the next trading session after ``ex_date``), matching the
        universe-membership availability contract (migration 009).

        Args:
            source_version: action-source version recorded on every row
                (e.g. a yfinance client/library version string). Required
                for the research-methodology action-source-version field
                (§2.3, §4); defaults to ``"unknown"`` for callers that have
                not yet been updated to supply one.

        Returns the number of rows written.
        """
        if df.empty:
            return 0

        required = {"ticker", "ex_date", "action_type", "value", "source"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"upsert_corporate_actions: missing required columns {missing}")

        from data.universe.calendar import conservative_known_at_for_date_only_source

        has_announced_col = "announced_at" in df.columns
        rows_written = 0

        for batch in _iter_batches(df, self._batch_size):
            rows = []
            for _, row in batch.iterrows():
                announced_at = row.get("announced_at") if has_announced_col else None
                if announced_at is not None and not pd.isna(announced_at):
                    known_at = announced_at
                    known_at_policy = "source_announced"
                else:
                    announced_at = None
                    known_at = conservative_known_at_for_date_only_source(row["ex_date"])
                    known_at_policy = "conservative_next_session"

                rows.append(
                    {
                        "ticker": row["ticker"],
                        "ex_date": row["ex_date"],
                        "action_type": row["action_type"],
                        "value": Decimal(str(row["value"])),
                        "notes": row.get("notes"),
                        "source": row["source"],
                        "announced_at": announced_at,
                        "known_at": known_at,
                        "known_at_policy": known_at_policy,
                        "source_version": source_version,
                    }
                )

            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO corporate_actions
                            (ticker, ex_date, action_type, value, notes, source,
                             announced_at, known_at, known_at_policy, source_version)
                        VALUES
                            (:ticker, :ex_date, :action_type, :value, :notes, :source,
                             :announced_at, :known_at, :known_at_policy, :source_version)
                        ON CONFLICT (ticker, ex_date, action_type) DO UPDATE SET
                            value            = EXCLUDED.value,
                            notes            = EXCLUDED.notes,
                            source           = EXCLUDED.source,
                            announced_at     = EXCLUDED.announced_at,
                            known_at         = EXCLUDED.known_at,
                            known_at_policy  = EXCLUDED.known_at_policy,
                            source_version   = EXCLUDED.source_version,
                            ingested_at      = NOW()
                        """
                    ),
                    rows,
                )
            rows_written += len(rows)

        logger.info("upsert_corporate_actions_complete", rows_written=rows_written)
        return rows_written

    def upsert_factor_scores(self, df: pd.DataFrame) -> int:
        """Upsert factor z-scores into factor_scores.

        Required columns: ticker, score_date, factor_name, strategy_id,
        z_score, research_run_id. Optional: raw_value.

        BUG-009 section 4 / migration 012: research_run_id is now part of
        the table's identity (primary key), not merely a label. Every row
        must be tagged with the research run that produced it so a new run
        can never silently UPSERT over an old methodology's rows — the
        ON CONFLICT target below includes research_run_id, so two runs that
        happen to score the same (ticker, score_date, factor_name,
        strategy_id) tuple insert two distinct rows instead of one
        overwriting the other.

        Returns the number of rows written.
        """
        if df.empty:
            return 0
        required = {"ticker", "score_date", "factor_name", "strategy_id", "z_score", "research_run_id"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"upsert_factor_scores: missing required columns {missing}")

        rows_written = 0
        for batch in _iter_batches(df, self._batch_size):
            rows = [
                {
                    "ticker": row["ticker"],
                    "score_date": row["score_date"],
                    "factor_name": row["factor_name"],
                    "strategy_id": row["strategy_id"],
                    "research_run_id": int(row["research_run_id"]),
                    "z_score": _to_decimal_or_none(row["z_score"]),
                    "raw_value": _to_decimal_or_none(row.get("raw_value")),
                }
                for _, row in batch.iterrows()
            ]
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO factor_scores
                            (ticker, score_date, factor_name, strategy_id, research_run_id, z_score, raw_value)
                        VALUES
                            (:ticker, :score_date, :factor_name, :strategy_id, :research_run_id, :z_score, :raw_value)
                        ON CONFLICT (ticker, score_date, factor_name, strategy_id, research_run_id) DO UPDATE SET
                            z_score     = EXCLUDED.z_score,
                            raw_value   = EXCLUDED.raw_value,
                            computed_at = NOW()
                        """
                    ),
                    rows,
                )
            rows_written += len(rows)

        logger.info("upsert_factor_scores_complete", rows_written=rows_written)
        return rows_written

    def upsert_alpha_scores(self, df: pd.DataFrame) -> int:
        """Upsert composite alpha scores into alpha_scores.

        Required columns: ticker, score_date, strategy_id, alpha_score,
        research_run_id. Optional: rank, universe_size.

        See :meth:`upsert_factor_scores` for why research_run_id is required
        and part of the ON CONFLICT target (BUG-009 section 4 / migration 012).

        Returns the number of rows written.
        """
        if df.empty:
            return 0
        required = {"ticker", "score_date", "strategy_id", "alpha_score", "research_run_id"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"upsert_alpha_scores: missing required columns {missing}")

        rows_written = 0
        for batch in _iter_batches(df, self._batch_size):
            rows = [
                {
                    "ticker": row["ticker"],
                    "score_date": row["score_date"],
                    "strategy_id": row["strategy_id"],
                    "research_run_id": int(row["research_run_id"]),
                    "alpha_score": _to_decimal_or_none(row["alpha_score"]),
                    "rank": _to_int_or_none(row.get("rank")),
                    "universe_size": _to_int_or_none(row.get("universe_size")),
                }
                for _, row in batch.iterrows()
            ]
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO alpha_scores
                            (ticker, score_date, strategy_id, research_run_id, alpha_score, rank, universe_size)
                        VALUES
                            (:ticker, :score_date, :strategy_id, :research_run_id, :alpha_score, :rank, :universe_size)
                        ON CONFLICT (ticker, score_date, strategy_id, research_run_id) DO UPDATE SET
                            alpha_score    = EXCLUDED.alpha_score,
                            rank           = EXCLUDED.rank,
                            universe_size  = EXCLUDED.universe_size,
                            computed_at    = NOW()
                        """
                    ),
                    rows,
                )
            rows_written += len(rows)

        logger.info("upsert_alpha_scores_complete", rows_written=rows_written)
        return rows_written

    def write_quality_flags(self, df: pd.DataFrame) -> int:
        """Write quality flags. Existing flags for the same (ticker, date, flag_type) are not updated
        — duplicate flags are silently skipped (ON CONFLICT DO NOTHING).
        """
        if df.empty:
            return 0

        rows_written = 0

        for batch in _iter_batches(df, self._batch_size):
            rows = [
                {
                    "ticker": row["ticker"],
                    "date": row["date"],
                    "flag_type": row["flag_type"],
                    "severity": row["severity"],
                    "message": row["message"],
                }
                for _, row in batch.iterrows()
            ]

            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO data_quality_flags (ticker, date, flag_type, severity, message)
                        VALUES (:ticker, :date, :flag_type, :severity, :message)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    rows,
                )
            rows_written += len(rows)

        return rows_written

    def log_ingestion(
        self,
        source: str,
        data_type: str,
        status: str,
        ticker: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        records_written: Optional[int] = None,
        error_message: Optional[str] = None,
        raw_storage_path: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> int:
        """Record a completed ingestion batch in data_ingestion_log.

        Returns the inserted row id.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    INSERT INTO data_ingestion_log
                        (batch_id, source, data_type, ticker, start_date, end_date,
                         records_written, status, error_message, raw_storage_path, completed_at)
                    VALUES
                        (:batch_id, :source, :data_type, :ticker, :start_date, :end_date,
                         :records_written, :status, :error_message, :raw_storage_path,
                         CASE WHEN :status != 'pending' THEN NOW() ELSE NULL END)
                    RETURNING id
                    """
                ),
                {
                    "batch_id": batch_id or str(uuid.uuid4()),
                    "source": source,
                    "data_type": data_type,
                    "ticker": ticker,
                    "start_date": start_date,
                    "end_date": end_date,
                    "records_written": records_written,
                    "status": status,
                    "error_message": error_message,
                    "raw_storage_path": raw_storage_path,
                },
            )
            row_id: int = result.scalar_one()

        return row_id

    # ─── Read helpers (used by pipeline checks) ───────────────────────────────

    def get_latest_ingestion_date(self, source: str, data_type: str) -> Optional[date]:
        """Return the most recent end_date in data_ingestion_log for a completed batch."""
        with self._engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT MAX(end_date) FROM data_ingestion_log
                    WHERE source = :source AND data_type = :data_type AND status = 'complete'
                    """
                ),
                {"source": source, "data_type": data_type},
            )
            val = result.scalar_one_or_none()
        return val

    def get_tickers_with_data(self, start: date, end: date) -> set[str]:
        """Return tickers that have daily_prices rows within 31 days of start.

        Used by the backfill CLI to skip tickers already present in the DB so
        an interrupted backfill can be resumed without re-fetching completed work.
        A ticker is considered 'done' if it has at least one row in the first 31
        calendar days of [start, end] — this tolerates weekends and public holidays
        while still catching genuine gaps for fresh tickers.
        """
        from datetime import timedelta
        cutoff = start + timedelta(days=31)
        with self._engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT DISTINCT ticker FROM daily_prices
                    WHERE date >= :start AND date < :cutoff
                    """
                ),
                {"start": start, "cutoff": cutoff},
            )
            return {row[0] for row in result}


# ─── Utilities ────────────────────────────────────────────────────────────────

def _to_decimal_or_none(value: object) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
        return None if (d.is_nan() or d.is_infinite()) else d
    except Exception:
        return None


def _to_int_or_none(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        f = float(str(value))
        return None if (f != f) else int(f)  # f != f is True for NaN
    except Exception:
        return None


def _iter_batches(df: pd.DataFrame, size: int):
    for start in range(0, len(df), size):
        yield df.iloc[start : start + size]
