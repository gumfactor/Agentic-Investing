"""Initial market schema: daily_prices, corporate_actions, ingestion log, quality flags.

Revision ID: 001
Revises:
Create Date: 2026-06-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "001"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── daily_prices ────────────────────────────────────────────────────────
    op.create_table(
        "daily_prices",
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(18, 6), nullable=True),
        sa.Column("high", sa.Numeric(18, 6), nullable=True),
        sa.Column("low", sa.Numeric(18, 6), nullable=True),
        sa.Column("close", sa.Numeric(18, 6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("source_adj_close", sa.Numeric(18, 6), nullable=True),
        sa.Column("source", sa.String(50), nullable=False, server_default="yfinance"),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("ticker", "date"),
    )
    op.create_index(
        "ix_daily_prices_ticker_date",
        "daily_prices",
        ["ticker", sa.text("date DESC")],
    )
    # Convert to TimescaleDB hypertable
    op.execute(
        "SELECT create_hypertable('daily_prices', 'date', "
        "chunk_time_interval => INTERVAL '1 month', if_not_exists => TRUE)"
    )

    # ── corporate_actions ───────────────────────────────────────────────────
    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("action_type", sa.String(20), nullable=False),
        sa.Column("value", sa.Numeric(18, 6), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source", sa.String(50), nullable=False, server_default="yfinance"),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker", "ex_date", "action_type"),
        sa.CheckConstraint(
            "action_type IN ('split', 'dividend', 'spinoff')",
            name="ck_corporate_actions_type",
        ),
    )
    op.create_index(
        "ix_corporate_actions_ticker_exdate",
        "corporate_actions",
        ["ticker", sa.text("ex_date DESC")],
    )

    # ── data_ingestion_log ──────────────────────────────────────────────────
    op.create_table(
        "data_ingestion_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("batch_id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("data_type", sa.String(50), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("records_written", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_storage_path", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'complete', 'failed')",
            name="ck_ingestion_log_status",
        ),
    )
    op.create_index(
        "ix_ingestion_log_started",
        "data_ingestion_log",
        [sa.text("started_at DESC")],
    )

    # ── data_quality_flags ──────────────────────────────────────────────────
    op.create_table(
        "data_quality_flags",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("flag_type", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default="FALSE"),
        sa.Column("resolved_by", sa.String(100), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "severity IN ('warning', 'error')",
            name="ck_quality_flags_severity",
        ),
    )
    op.create_index(
        "ix_quality_flags_ticker_date",
        "data_quality_flags",
        ["ticker", sa.text("date DESC")],
    )
    op.create_index(
        "ix_quality_flags_unresolved",
        "data_quality_flags",
        ["resolved", "severity"],
        postgresql_where=sa.text("resolved = FALSE"),
    )


def downgrade() -> None:
    # Drop in reverse dependency order.
    # Per C2 in PRD.md: all schema changes go through migrations; no raw DDL in prod.
    op.drop_table("data_quality_flags")
    op.drop_table("data_ingestion_log")
    op.drop_table("corporate_actions")
    op.drop_table("daily_prices")
