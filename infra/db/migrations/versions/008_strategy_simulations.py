"""Create strategy_simulations table.

Required by Dashboard Sprint 3 — Page 5 (Performance) strategy comparison.
The Airflow signal pipeline computes forward simulations for shadow strategies.

Revision ID: 008
Revises: 007
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_simulations",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("strategy_id", sa.Text, nullable=False),
        sa.Column("sim_date", sa.Date, nullable=False),
        sa.Column("target_weights", postgresql.JSONB, nullable=False),
        sa.Column("simulated_return", sa.Numeric(12, 8), nullable=False),
        sa.Column("simulated_nav", sa.Numeric(18, 6), nullable=False),
        sa.Column("universe_size", sa.Integer, nullable=False),
        sa.Column("n_positions", sa.Integer, nullable=False),
        sa.Column("computed_at_utc", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("strategy_id", "sim_date", name="uq_strategy_simulations_sid_date"),
    )
    op.create_index(
        "ix_strategy_simulations_date_strategy",
        "strategy_simulations",
        ["sim_date", "strategy_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_simulations_date_strategy")
    op.drop_table("strategy_simulations")
