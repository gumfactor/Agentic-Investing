"""Versioned research identity: research_methodologies, research_runs,
research_run_id on signal_ic_stats/factor_scores/alpha_scores.

BUG-009 section 4 (docs/plans/01b-research-validity-design.md). Before this
migration, ``signal_ic_stats.provisional`` (migration 010) was the only
identity marker distinguishing PIT-safe research from legacy/current-
membership research, and it could not answer "which timing policy, which
corporate-action availability policy, which data snapshot produced this
row" — nor could it prevent a new research run from silently UPSERTing over
an old methodology's rows, since the unique constraints on all three tables
predate any run/methodology concept.

This migration:

1. Creates ``research_methodologies`` (universe import/version + availability
   policy, timing policy, score/realized-return corporate-action
   availability policies, action-source version, return/adjustment policy,
   missing-data policy, code/config hash) and ``research_runs``
   (references a methodology + a data version, C7).
2. Adds ``research_run_id`` (FK, NOT NULL after backfill) to
   ``signal_ic_stats``, ``factor_scores``, and ``alpha_scores``, and widens
   each table's unique constraint / primary key to include it, so a new run
   can never silently overwrite an old methodology's rows via the writers'
   existing ``ON CONFLICT`` upserts.
3. Backfills exactly one legacy methodology + legacy run
   (``research_runs.status = 'legacy_provisional'``,
   ``is_active = FALSE``) and points every pre-existing row at it.

Design decision on migration 010's ``provisional`` boolean (documented per
the plan's instruction to record this choice): ``signal_ic_stats.provisional``
is KEPT, unmodified, for backward read-compatibility with any code still
querying it directly. It is not extended further and is not the
authoritative marker going forward — that is now
``research_runs.status`` / ``research_runs.is_active``, reached via
``research_run_id``. The legacy backfill run below has
``status='legacy_provisional'``, matching the historical meaning of
``provisional=TRUE`` for the same rows, so the two markers agree for every
row created before this migration; they do not need to be reconciled
further because migration 010's column is frozen at this point (see
its docstring, which already declared it superseded by this work).

Revision ID: 012
Revises: 011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: str = "011"
branch_labels = None
depends_on = None

_LEGACY_METHODOLOGY_NAME = "legacy_provisional_pre_01b3"
_LEGACY_RUN_LABEL = "legacy_provisional_backfill"
_LEGACY_TIMING_POLICY_ID = "legacy_same_close_v0"
_LEGACY_POLICY_VALUE = "legacy_unknown"


def upgrade() -> None:
    # ── research_methodologies ──────────────────────────────────────────────
    op.create_table(
        "research_methodologies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("universe_id", sa.Text(), nullable=True),
        sa.Column("universe_import_policy", sa.Text(), nullable=False),
        sa.Column("timing_policy_id", sa.Text(), nullable=False),
        sa.Column("score_action_availability_policy", sa.Text(), nullable=False),
        sa.Column("realized_return_action_availability_policy", sa.Text(), nullable=False),
        sa.Column("action_source_version", sa.Text(), nullable=False),
        sa.Column("return_adjustment_policy", sa.Text(), nullable=False),
        sa.Column("missing_data_policy", sa.Text(), nullable=False),
        sa.Column("code_config_hash", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_research_methodologies_name"),
    )

    # ── research_runs ────────────────────────────────────────────────────────
    op.create_table(
        "research_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("methodology_id", sa.BigInteger(), nullable=False),
        sa.Column("data_version", sa.Text(), nullable=False),
        sa.Column("run_label", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="candidate"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")
        ),
        sa.Column("activated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("activated_by", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["methodology_id"], ["research_methodologies.id"],
            name="fk_research_runs_methodology", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('legacy_provisional', 'candidate', 'active', 'superseded', 'rejected')",
            name="ck_research_runs_status",
        ),
    )
    op.create_index("ix_research_runs_methodology", "research_runs", ["methodology_id"])
    # At most one active run per methodology (section 4 item 2: an explicit
    # active run, never assumed to be the newest row).
    op.execute(
        "CREATE UNIQUE INDEX ux_research_runs_one_active_per_methodology "
        "ON research_runs (methodology_id) WHERE is_active"
    )

    # ── Legacy methodology + run backfill ───────────────────────────────────
    connection = op.get_bind()
    methodology_id = connection.execute(
        sa.text(
            "INSERT INTO research_methodologies "
            "(name, universe_import_policy, timing_policy_id, "
            " score_action_availability_policy, realized_return_action_availability_policy, "
            " action_source_version, return_adjustment_policy, missing_data_policy, "
            " code_config_hash, notes) "
            "VALUES (:name, :universe_import_policy, :timing_policy_id, "
            " :score_policy, :realized_policy, :action_source_version, "
            " :return_policy, :missing_data_policy, :code_hash, :notes) "
            "RETURNING id"
        ),
        {
            "name": _LEGACY_METHODOLOGY_NAME,
            "universe_import_policy": "legacy_current_membership_no_pit_enforcement",
            "timing_policy_id": _LEGACY_TIMING_POLICY_ID,
            "score_policy": _LEGACY_POLICY_VALUE,
            "realized_policy": _LEGACY_POLICY_VALUE,
            "action_source_version": _LEGACY_POLICY_VALUE,
            "return_policy": "legacy_same_close_unadjusted",
            "missing_data_policy": _LEGACY_POLICY_VALUE,
            "code_hash": _LEGACY_POLICY_VALUE,
            "notes": (
                "Migrated placeholder for every signal_ic_stats/factor_scores/"
                "alpha_scores row written before 01B-3 (docs/plans/"
                "01b-research-validity-design.md section 4). These rows used the "
                "current-membership universe (BUG-008) and/or the same-close "
                "return convention (BUG-009) and are not valid for selection, "
                "promotion, or paper-trading qualification."
            ),
        },
    ).scalar_one()

    legacy_run_id = connection.execute(
        sa.text(
            "INSERT INTO research_runs "
            "(methodology_id, data_version, run_label, status, is_active, notes) "
            "VALUES (:methodology_id, :data_version, :run_label, 'legacy_provisional', FALSE, :notes) "
            "RETURNING id"
        ),
        {
            "methodology_id": methodology_id,
            "data_version": "legacy_unversioned",
            "run_label": _LEGACY_RUN_LABEL,
            "notes": "Backfill target for all pre-01B-3 rows; never active.",
        },
    ).scalar_one()

    # ── signal_ic_stats ──────────────────────────────────────────────────────
    op.add_column("signal_ic_stats", sa.Column("research_run_id", sa.BigInteger(), nullable=True))
    connection.execute(
        sa.text("UPDATE signal_ic_stats SET research_run_id = :run_id WHERE research_run_id IS NULL"),
        {"run_id": legacy_run_id},
    )
    op.alter_column("signal_ic_stats", "research_run_id", nullable=False)
    op.create_foreign_key(
        "fk_signal_ic_stats_research_run", "signal_ic_stats", "research_runs",
        ["research_run_id"], ["id"], ondelete="RESTRICT",
    )
    op.drop_constraint("uq_ic_stats_key", "signal_ic_stats", type_="unique")
    op.create_unique_constraint(
        "uq_ic_stats_key_run",
        "signal_ic_stats",
        ["research_run_id", "factor_name", "strategy_id", "eval_date", "horizon_days"],
    )
    op.create_index("ix_signal_ic_stats_research_run", "signal_ic_stats", ["research_run_id"])

    # ── factor_scores (hypertable: PK must keep the partition column score_date) ──
    op.add_column("factor_scores", sa.Column("research_run_id", sa.BigInteger(), nullable=True))
    connection.execute(
        sa.text("UPDATE factor_scores SET research_run_id = :run_id WHERE research_run_id IS NULL"),
        {"run_id": legacy_run_id},
    )
    op.alter_column("factor_scores", "research_run_id", nullable=False)
    op.execute("ALTER TABLE factor_scores DROP CONSTRAINT pk_factor_scores")
    op.execute(
        "ALTER TABLE factor_scores ADD CONSTRAINT pk_factor_scores "
        "PRIMARY KEY (ticker, score_date, factor_name, strategy_id, research_run_id)"
    )
    op.create_foreign_key(
        "fk_factor_scores_research_run", "factor_scores", "research_runs",
        ["research_run_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index("ix_factor_scores_research_run", "factor_scores", ["research_run_id"])

    # ── alpha_scores (hypertable: PK must keep the partition column score_date) ──
    op.add_column("alpha_scores", sa.Column("research_run_id", sa.BigInteger(), nullable=True))
    connection.execute(
        sa.text("UPDATE alpha_scores SET research_run_id = :run_id WHERE research_run_id IS NULL"),
        {"run_id": legacy_run_id},
    )
    op.alter_column("alpha_scores", "research_run_id", nullable=False)
    op.execute("ALTER TABLE alpha_scores DROP CONSTRAINT pk_alpha_scores")
    op.execute(
        "ALTER TABLE alpha_scores ADD CONSTRAINT pk_alpha_scores "
        "PRIMARY KEY (ticker, score_date, strategy_id, research_run_id)"
    )
    op.create_foreign_key(
        "fk_alpha_scores_research_run", "alpha_scores", "research_runs",
        ["research_run_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index("ix_alpha_scores_research_run", "alpha_scores", ["research_run_id"])


def downgrade() -> None:
    op.drop_index("ix_alpha_scores_research_run", table_name="alpha_scores")
    op.drop_constraint("fk_alpha_scores_research_run", "alpha_scores", type_="foreignkey")
    op.execute("ALTER TABLE alpha_scores DROP CONSTRAINT pk_alpha_scores")
    op.execute(
        "ALTER TABLE alpha_scores ADD CONSTRAINT pk_alpha_scores "
        "PRIMARY KEY (ticker, score_date, strategy_id)"
    )
    op.drop_column("alpha_scores", "research_run_id")

    op.drop_index("ix_factor_scores_research_run", table_name="factor_scores")
    op.drop_constraint("fk_factor_scores_research_run", "factor_scores", type_="foreignkey")
    op.execute("ALTER TABLE factor_scores DROP CONSTRAINT pk_factor_scores")
    op.execute(
        "ALTER TABLE factor_scores ADD CONSTRAINT pk_factor_scores "
        "PRIMARY KEY (ticker, score_date, factor_name, strategy_id)"
    )
    op.drop_column("factor_scores", "research_run_id")

    op.drop_index("ix_signal_ic_stats_research_run", table_name="signal_ic_stats")
    op.drop_constraint("uq_ic_stats_key_run", "signal_ic_stats", type_="unique")
    op.create_unique_constraint(
        "uq_ic_stats_key", "signal_ic_stats",
        ["factor_name", "strategy_id", "eval_date", "horizon_days"],
    )
    op.drop_constraint("fk_signal_ic_stats_research_run", "signal_ic_stats", type_="foreignkey")
    op.drop_column("signal_ic_stats", "research_run_id")

    op.execute("DROP INDEX IF EXISTS ux_research_runs_one_active_per_methodology")
    op.drop_index("ix_research_runs_methodology", table_name="research_runs")
    op.drop_table("research_runs")
    op.drop_table("research_methodologies")
