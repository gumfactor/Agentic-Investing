"""Trade journal schema: trade_fills append-only fill store.

Revision ID: 004
Revises: 003
Create Date: 2026-06-23

Implements Phase 5, M5.2 — the durable fill store required to:
  - compute FIFO realized P&L
  - provide real wash-sale history to ComplianceEngine (unblocks the Phase 4 stub)
  - reconstruct open position cost-basis

Safety rule C3: this table is INSERT-only.  No UPDATE or DELETE paths exist
anywhere in the application layer.  All correction records use a separate
``correction_of`` reference (not implemented yet) rather than mutating rows.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "004"
down_revision: str = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── trade_fills ──────────────────────────────────────────────────────────
    # Append-only fill store (C3).  One row per fill event (FILLED or
    # PARTIALLY_FILLED transition recorded by TradeJournal.record_fill()).
    op.create_table(
        "trade_fills",
        sa.Column("fill_id", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "side",
            sa.Text(),
            nullable=False,
            comment="BUY or SELL",
        ),
        sa.Column("filled_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("avg_fill_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("limit_price", sa.Numeric(18, 6), nullable=True),
        sa.Column(
            "fill_timestamp",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            comment="When the OMS confirmed this fill; partition key for hypertable",
        ),
        sa.Column(
            "order_status_at_record",
            sa.Text(),
            nullable=False,
            comment="Order.status value at the time of recording (FILLED or PARTIALLY_FILLED)",
        ),
        sa.Column(
            "realized_pnl",
            sa.Numeric(18, 6),
            nullable=True,
            comment="NULL for BUYs; FIFO realized P&L for SELLs",
        ),
        sa.Column(
            "cost_basis_per_share",
            sa.Numeric(18, 6),
            nullable=True,
            comment="NULL for BUYs; FIFO weighted avg cost for SELLs",
        ),
        sa.Column(
            "wash_sale_disallowed",
            sa.Boolean(),
            nullable=False,
            server_default="FALSE",
            comment="Reserved for future wash-sale disallowance tagging",
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("fill_id"),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_trade_fills_side"),
        sa.CheckConstraint("filled_quantity > 0", name="ck_trade_fills_qty_positive"),
        sa.CheckConstraint("avg_fill_price > 0", name="ck_trade_fills_price_positive"),
    )

    # Dedup guard: one row per (order_id, filled_quantity) prevents re-recording
    # the same fill event if reconcile_fills() is called twice before the order
    # transitions to a terminal status.
    op.create_unique_constraint(
        "uq_trade_fills_order_qty",
        "trade_fills",
        ["order_id", "filled_quantity"],
    )

    # Ticker + time index for fill history queries and FIFO lot reconstruction.
    op.create_index(
        "ix_trade_fills_ticker_time",
        "trade_fills",
        ["ticker", sa.text("fill_timestamp DESC")],
    )
    # Strategy + time index for P&L summary queries.
    op.create_index(
        "ix_trade_fills_strategy_time",
        "trade_fills",
        ["strategy_id", sa.text("fill_timestamp DESC")],
    )
    # Partial index for wash-sale lookups: only loss-realizing SELLs matter.
    op.create_index(
        "ix_trade_fills_wash_sale",
        "trade_fills",
        ["ticker", sa.text("fill_timestamp DESC")],
        postgresql_where=sa.text("side = 'SELL' AND realized_pnl < 0"),
    )

    # Convert to TimescaleDB hypertable.  chunk_time_interval=1 month keeps
    # chunk count reasonable for the expected fill volume.
    # if_not_exists=TRUE prevents failures on re-runs (e.g. partial migrations).
    op.execute(
        "SELECT create_hypertable('trade_fills', 'fill_timestamp', "
        "chunk_time_interval => INTERVAL '1 month', if_not_exists => TRUE)"
    )


def downgrade() -> None:
    op.drop_table("trade_fills")
