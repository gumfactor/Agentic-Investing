"""Signal schema: factor_scores, alpha_scores, signal_ic_stats.

Revision ID: 002
Revises: 001
Create Date: 2026-06-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: str = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── factor_scores ────────────────────────────────────────────────────────
    # One row per (ticker, score_date, factor_name, strategy_id).
    # z_score is the cross-sectional z-score; raw_value is the un-normalized
    # underlying metric (e.g. 12-month return, realized vol).
    op.create_table(
        "factor_scores",
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("score_date", sa.Date(), nullable=False),
        sa.Column("factor_name", sa.String(100), nullable=False),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("z_score", sa.Numeric(10, 6), nullable=True),
        sa.Column("raw_value", sa.Numeric(18, 6), nullable=True),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("ticker", "score_date", "factor_name", "strategy_id"),
    )
    op.create_index(
        "ix_factor_scores_date_factor",
        "factor_scores",
        ["score_date", "factor_name", "strategy_id"],
    )
    op.create_index(
        "ix_factor_scores_ticker",
        "factor_scores",
        ["ticker", sa.text("score_date DESC")],
    )
    op.execute(
        "SELECT create_hypertable('factor_scores', 'score_date', "
        "chunk_time_interval => INTERVAL '3 months', if_not_exists => TRUE)"
    )

    # ── alpha_scores ─────────────────────────────────────────────────────────
    # Composite signal score for each (ticker, date, strategy).
    # rank is 1-based within the universe on that date (1 = highest score).
    op.create_table(
        "alpha_scores",
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("score_date", sa.Date(), nullable=False),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("alpha_score", sa.Numeric(10, 6), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("universe_size", sa.Integer(), nullable=True),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("ticker", "score_date", "strategy_id"),
    )
    op.create_index(
        "ix_alpha_scores_date_strategy",
        "alpha_scores",
        ["score_date", "strategy_id"],
    )
    op.create_index(
        "ix_alpha_scores_ticker",
        "alpha_scores",
        ["ticker", sa.text("score_date DESC")],
    )
    op.execute(
        "SELECT create_hypertable('alpha_scores', 'score_date', "
        "chunk_time_interval => INTERVAL '3 months', if_not_exists => TRUE)"
    )

    # ── signal_ic_stats ──────────────────────────────────────────────────────
    # IC metrics per (factor, horizon, eval_date, strategy).
    # eval_date = last date of the evaluation window used to compute the stats.
    # Used by the signal_research skill and logged to MLflow.
    op.create_table(
        "signal_ic_stats",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("factor_name", sa.String(100), nullable=False),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("eval_date", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("ic", sa.Numeric(10, 6), nullable=True),
        sa.Column("rank_ic", sa.Numeric(10, 6), nullable=True),
        sa.Column("ic_tstat", sa.Numeric(10, 6), nullable=True),
        sa.Column("ic_ir", sa.Numeric(10, 6), nullable=True),
        sa.Column("ic_pvalue", sa.Numeric(10, 6), nullable=True),
        sa.Column("n_observations", sa.Integer(), nullable=True),
        sa.Column("mlflow_run_id", sa.String(100), nullable=True),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "factor_name", "strategy_id", "eval_date", "horizon_days",
            name="uq_ic_stats_factor_strategy_date_horizon",
        ),
    )
    op.create_index(
        "ix_ic_stats_factor_date",
        "signal_ic_stats",
        ["factor_name", sa.text("eval_date DESC")],
    )


def downgrade() -> None:
    op.drop_table("signal_ic_stats")
    op.drop_table("alpha_scores")
    op.drop_table("factor_scores")
