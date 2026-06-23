"""Strategy registry: strategy_definitions, strategies, strategy_runs, strategy_status_history.

Revision ID: 004
Revises: 003
Create Date: 2026-06-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: str = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── strategy_definitions ─────────────────────────────────────────────────
    # Research layer. One row per (strategy_id, config_hash).
    # Multiple rows per strategy_id expected during pre-registration iteration.
    op.create_table(
        "strategy_definitions",
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("portfolio_method", sa.Text(), nullable=True),
        sa.Column("n_long", sa.Integer(), nullable=True),
        sa.Column("rebalance_frequency", sa.Text(), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("strategy_id", "config_hash", name="pk_strategy_definitions"),
        sa.UniqueConstraint(
            "strategy_id", "version", name="uq_strategy_definitions_version"
        ),
        sa.CheckConstraint(
            "strategy_id ~ '^[a-z][a-z0-9_]{2,99}$'",
            name="ck_strategy_definitions_strategy_id",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_strategy_definitions_version_positive",
        ),
        sa.CheckConstraint(
            "length(config_hash) = 64",
            name="ck_strategy_definitions_hash_length",
        ),
    )
    op.create_index(
        "ix_strategy_definitions_strategy_id",
        "strategy_definitions",
        ["strategy_id"],
    )
    op.create_index(
        "ix_strategy_definitions_created",
        "strategy_definitions",
        [sa.text("created_at DESC")],
    )

    # ── strategies ───────────────────────────────────────────────────────────
    # Operational/lifecycle layer. One row per strategy_id.
    # Pins the canonical config hash; carries the status state machine.
    op.create_table(
        "strategies",
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("canonical_config_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("strategy_family", sa.Text(), nullable=True),
        sa.Column("supersedes_strategy_id", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("strategy_id", name="pk_strategies"),
        sa.ForeignKeyConstraint(
            ["strategy_id", "canonical_config_hash"],
            ["strategy_definitions.strategy_id", "strategy_definitions.config_hash"],
            name="fk_strategies_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_strategy_id"],
            ["strategies.strategy_id"],
            name="fk_strategies_supersedes",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('backtesting', 'paper', 'live', 'archived')",
            name="ck_strategies_status",
        ),
        sa.CheckConstraint(
            "strategy_id ~ '^[a-z][a-z0-9_]{2,99}$'",
            name="ck_strategies_strategy_id",
        ),
    )
    op.create_index("ix_strategies_status", "strategies", ["status"])
    op.create_index("ix_strategies_family", "strategies", ["strategy_family"])

    # At most one strategy in paper at a time.
    op.execute(
        "CREATE UNIQUE INDEX uix_strategies_one_paper ON strategies (status) WHERE status = 'paper'"
    )
    # At most one strategy in live at a time.
    op.execute(
        "CREATE UNIQUE INDEX uix_strategies_one_live ON strategies (status) WHERE status = 'live'"
    )

    # ── strategy_runs ────────────────────────────────────────────────────────
    # Append-only record of every experiment run against a (strategy_id, config_hash).
    # Can be written before formal registration. Links to strategy_definitions, not strategies.
    op.create_table(
        "strategy_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("run_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data_version", sa.Text(), nullable=True),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("mlflow_run_id", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_runs"),
        sa.ForeignKeyConstraint(
            ["strategy_id", "config_hash"],
            ["strategy_definitions.strategy_id", "strategy_definitions.config_hash"],
            name="fk_strategy_runs_definition",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "run_type IN ('unit', 'signal_ic', 'backtest', 'walk_forward', 'paper', 'live')",
            name="ck_strategy_runs_run_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'passed', 'failed', 'blocked')",
            name="ck_strategy_runs_status",
        ),
    )
    op.create_index(
        "ix_strategy_runs_strategy_started",
        "strategy_runs",
        ["strategy_id", sa.text("started_at DESC")],
    )
    op.create_index(
        "ix_strategy_runs_type_status",
        "strategy_runs",
        ["run_type", "status"],
    )

    # ── strategy_status_history ──────────────────────────────────────────────
    # Append-only audit trail of every lifecycle transition. Never updated or deleted (C3).
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


def downgrade() -> None:
    op.drop_table("strategy_status_history")
    op.drop_table("strategy_runs")
    op.drop_index("uix_strategies_one_live", table_name="strategies")
    op.drop_index("uix_strategies_one_paper", table_name="strategies")
    op.drop_index("ix_strategies_family", table_name="strategies")
    op.drop_index("ix_strategies_status", table_name="strategies")
    op.drop_table("strategies")
    op.drop_index("ix_strategy_definitions_created", table_name="strategy_definitions")
    op.drop_index("ix_strategy_definitions_strategy_id", table_name="strategy_definitions")
    op.drop_table("strategy_definitions")
