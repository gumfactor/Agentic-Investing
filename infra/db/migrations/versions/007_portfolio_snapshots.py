"""Create portfolio_snapshots table.

Required by Dashboard Sprint 2 — Page 2 (Positions & P&L).
The Airflow DAG's fetch_ibkr_snapshot task writes snapshots here.

Revision ID: 007
Revises: 006
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("snapshot_date", sa.Date, nullable=False),
        sa.Column("strategy_id", sa.Text, nullable=False),
        sa.Column("dag_run_id", sa.Text, nullable=False),
        sa.Column("fetched_at_utc", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("cash_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("positions", postgresql.JSONB, nullable=False),
        sa.Column("nav_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("source", sa.Text, server_default="ibkr_paper", nullable=False),
        sa.UniqueConstraint("snapshot_date", "strategy_id", name="uq_portfolio_snapshots_date_strategy"),
    )
    op.create_index(
        "ix_portfolio_snapshots_date_strategy",
        "portfolio_snapshots",
        ["snapshot_date", "strategy_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_portfolio_snapshots_date_strategy")
    op.drop_table("portfolio_snapshots")
