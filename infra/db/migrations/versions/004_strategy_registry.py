"""Strategy Registry: strategies, strategy_status_history, strategy_performance_snapshots.

Revision ID: 004
Revises: 003
Create Date: 2026-06-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: str = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── strategies ──────────────────────────────────────────────────────────
    op.create_table(
        "strategies",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("config_path", sa.Text(), nullable=False),
        sa.Column("config_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("strategy_family", sa.Text(), nullable=True),
        sa.Column("supersedes_strategy_id", sa.Text(), nullable=True),
        sa.Column("portfolio_method", sa.Text(), nullable=True),
        sa.Column("n_long", sa.Integer(), nullable=True),
        sa.Column("rebalance_frequency", sa.Text(), nullable=True),
        sa.Column(
            "registered_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_paper_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("activated_live_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_strategies"),
        sa.UniqueConstraint("strategy_id", name="uq_strategies_strategy_id"),
        sa.CheckConstraint(
            "status IN ('backtesting', 'paper', 'live', 'archived')",
            name="ck_strategies_status",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_strategy_id"],
            ["strategies.strategy_id"],
            name="fk_strategies_supersedes",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_strategies_status", "strategies", ["status"])
    op.create_index("ix_strategies_family", "strategies", ["strategy_family"])

    # Enforce at most one strategy in paper at a time.
    op.execute(
        """
        CREATE UNIQUE INDEX uix_strategies_one_paper
        ON strategies (status)
        WHERE status = 'paper'
        """
    )

    # Enforce at most one strategy in live at a time.
    op.execute(
        """
        CREATE UNIQUE INDEX uix_strategies_one_live
        ON strategies (status)
        WHERE status = 'live'
        """
    )

    # ── strategy_status_history ─────────────────────────────────────────────
    op.create_table(
        "strategy_status_history",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column(
            "transitioned_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("operator_notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_status_history"),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.strategy_id"],
            name="fk_strategy_status_history_strategy",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_strategy_status_history_strategy_id",
        "strategy_status_history",
        ["strategy_id", sa.text("transitioned_at DESC")],
    )

    # ── strategy_performance_snapshots ──────────────────────────────────────
    op.create_table(
        "strategy_performance_snapshots",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("period_type", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("annualized_return", sa.Numeric(18, 6), nullable=True),
        sa.Column("annualized_volatility", sa.Numeric(18, 6), nullable=True),
        sa.Column("sharpe_ratio", sa.Numeric(18, 6), nullable=True),
        sa.Column("max_drawdown", sa.Numeric(18, 6), nullable=True),
        sa.Column("information_ratio", sa.Numeric(18, 6), nullable=True),
        sa.Column("total_trades", sa.Integer(), nullable=True),
        sa.Column("data_version", sa.Text(), nullable=True),
        sa.Column("mlflow_run_id", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_performance_snapshots"),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.strategy_id"],
            name="fk_strategy_perf_strategy",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "period_type IN ('backtest', 'paper', 'live')",
            name="ck_strategy_perf_period_type",
        ),
    )
    op.create_index(
        "ix_strategy_perf_strategy_period",
        "strategy_performance_snapshots",
        ["strategy_id", "period_type", sa.text("snapshot_date DESC")],
    )


def downgrade() -> None:
    op.drop_table("strategy_performance_snapshots")
    op.drop_table("strategy_status_history")
    op.drop_index("uix_strategies_one_live", table_name="strategies")
    op.drop_index("uix_strategies_one_paper", table_name="strategies")
    op.drop_index("ix_strategies_family", table_name="strategies")
    op.drop_index("ix_strategies_status", table_name="strategies")
    op.drop_table("strategies")
