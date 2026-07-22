"""Strategy-selection protocol schema: strategy_hypotheses, strategy_trials,
research_data_windows, promotion_decisions.

Roadmap Gate 04, slice 04-1 (docs/plans/04-strategy-selection-protocol-design.md
§5.1, §7 row 04-1). Pure schema — no runtime behavior. This migration lays the
durable trial/hypothesis registry and the train/OOS/holdout window and
promotion-decision audit tables that later slices (04-2 ``TrialRecorder``,
04-3 hypothesis write path, 04-4 ``PromotionPipeline``, 04-5 the new
``validated`` Strategy Registry status) build on top of. See the design doc's
§8 for the operator's 2026-07-22 resolutions this schema encodes:

- Q1: ``research_data_windows`` scoping is PER-STRATEGY by default (the
  nullable ``strategy_family``/``strategy_id`` columns still support a
  per-family override — the scope-XOR CHECK below allows either, never both,
  never neither).
- Q3: ``promotion_decisions.dsr_value`` is informational only — no DSR floor
  is enforced by any CHECK constraint here; ``overall_passed`` is an
  independently-set boolean column, not derived from ``dsr_value`` at the
  DB layer.
- Q6: the per-family Benjamini-Hochberg FDR scope is an application-layer
  query concern (``strategy_trials``/``strategy_definitions`` already carry
  enough identity to join across a family); no schema change is needed for
  it here.

``strategy_hypotheses.hypothesis_id`` FK-nullability and the
``frozen_at``/``param_grid_json`` immutability-after-first-trial rule are
enforced in the application layer (04-2/04-3's ``TrialRecorder``), matching
this repository's existing precedent of ``verify_config_integrity()`` being
an application-layer check rather than a DB trigger (see
``strategy_registry/registry.py``, cited by this migration's design doc §4.1).

FK target verification: ``strategy_definitions`` (migration
``004_strategy_registry.py``) has a composite PRIMARY KEY on
``(strategy_id, config_hash)`` (see that migration's
``sa.PrimaryKeyConstraint("strategy_id", "config_hash", ...)`` and the ORM
mirror in ``strategy_registry/models.py``'s ``StrategyDefinition``), so the
``strategy_trials.config_hash`` and ``promotion_decisions.config_hash``
composite foreign keys below reference it exactly as designed in §5.1 — no
deviation from the design doc was required.

Revision ID: 014
Revises: 013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "014"
down_revision: str = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── strategy_hypotheses ──────────────────────────────────────────────────
    # Pre-registered research questions, written before any candidate config
    # is run (§5.1). No FK to strategy_definitions/strategies -- a hypothesis
    # can precede either.
    op.create_table(
        "strategy_hypotheses",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("hypothesis_text", sa.Text(), nullable=False),
        sa.Column(
            "param_grid_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("frozen_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_hypotheses"),
        sa.CheckConstraint(
            "length(strategy_id) > 0",
            name="ck_strategy_hypotheses_strategy_id_nonempty",
        ),
        # Composite unique on (id, strategy_id). `id` is already the PK so this
        # is trivially satisfied; it exists solely to serve as the target of
        # strategy_trials' composite (hypothesis_id, strategy_id) FK below, so
        # a trial can only cite a hypothesis pre-registered for the SAME
        # strategy_id (audit-trail integrity -- a trial for strategy A must not
        # count under A while referencing strategy B's pre-registration).
        sa.UniqueConstraint(
            "id", "strategy_id", name="uq_strategy_hypotheses_id_strategy_id"
        ),
    )
    op.create_index(
        "ix_strategy_hypotheses_strategy_id",
        "strategy_hypotheses",
        ["strategy_id"],
    )

    # ── strategy_trials ──────────────────────────────────────────────────────
    # Append-only (C3-style discipline, mirrors strategy_runs): one row per
    # candidate run attempt, inserted before the wrapped instrument dispatches
    # (04-2) so a crashed/discarded run still counts toward n_trials.
    op.create_table(
        "strategy_trials",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("hypothesis_id", sa.BigInteger(), nullable=True),
        sa.Column("window", sa.Text(), nullable=False),
        sa.Column("run_type", sa.Text(), nullable=False),
        sa.Column("data_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("oos_sharpe", sa.Numeric(), nullable=True),
        sa.Column("oos_max_drawdown", sa.Numeric(), nullable=True),
        sa.Column(
            "metrics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("mlflow_run_id", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_trials"),
        sa.ForeignKeyConstraint(
            ["strategy_id", "config_hash"],
            ["strategy_definitions.strategy_id", "strategy_definitions.config_hash"],
            name="fk_strategy_trials_definition",
            ondelete="RESTRICT",
        ),
        # Composite FK: a trial's (hypothesis_id, strategy_id) must match a
        # single strategy_hypotheses row's (id, strategy_id) -- so a trial can
        # only reference a hypothesis pre-registered for its OWN strategy_id.
        # hypothesis_id remains nullable; under default MATCH SIMPLE semantics
        # a NULL hypothesis_id skips FK enforcement entirely, preserving the
        # documented legacy-backfill path (a pre-protocol trial with no
        # hypothesis).
        sa.ForeignKeyConstraint(
            ["hypothesis_id", "strategy_id"],
            ["strategy_hypotheses.id", "strategy_hypotheses.strategy_id"],
            name="fk_strategy_trials_hypothesis",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            '"window" IN (\'train_oos\', \'holdout\')',
            name="ck_strategy_trials_window",
        ),
        sa.CheckConstraint(
            "run_type IN ('walk_forward', 'parameter_sweep_variant', "
            "'holdout_confirmation')",
            name="ck_strategy_trials_run_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'errored')",
            name="ck_strategy_trials_status",
        ),
        # Couple window and run_type so they cannot disagree: a row touches the
        # holdout window IFF it is a holdout_confirmation run. Without this, a
        # recorder bug or manual backfill could label a run as window='holdout'
        # but run_type='walk_forward'/'parameter_sweep_variant', which the
        # one-shot seal below (keyed on run_type='holdout_confirmation') would
        # NOT catch -- allowing unlimited looks at the sealed holdout data. With
        # this biconditional, every holdout-window row is a holdout_confirmation
        # row, so the run_type-keyed seal provably covers every holdout touch.
        # Boolean-equality form works identically on Postgres and SQLite.
        sa.CheckConstraint(
            "(\"window\" = 'holdout') = (run_type = 'holdout_confirmation')",
            name="ck_strategy_trials_holdout_window_iff_confirmation",
        ),
        # NaN backstop (Postgres-only): Postgres `numeric` DOES support NaN
        # (`'NaN'::numeric` inserts and persists), so an unfiltered
        # float('nan')/numpy.nan written by 04-2's TrialRecorder would
        # silently poison downstream DSR/funnel/stress comparisons. Reject
        # NaN while still allowing NULL (the legitimate "not computed yet"
        # value while status='running'). The self-comparison idiom `col =
        # col` does NOT work here: Postgres defines `NaN = NaN` as TRUE for
        # numeric, so it passes NaN through; `col <> 'NaN'::numeric` is FALSE
        # for a NaN (so the CHECK fails and NaN is rejected) and TRUE for any
        # real value. ddl_if(postgresql) because the `::numeric` cast is a
        # Postgres syntax error on SQLite, and SQLite coerces float('nan') to
        # NULL on storage anyway (so it cannot poison the SQLite test path) --
        # see selection_models.py's mirrored constraints and the app-layer
        # NaN->NULL commitment note there.
        sa.CheckConstraint(
            "oos_sharpe IS NULL OR oos_sharpe <> 'NaN'::numeric",
            name="ck_strategy_trials_oos_sharpe_not_nan",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "oos_max_drawdown IS NULL OR oos_max_drawdown <> 'NaN'::numeric",
            name="ck_strategy_trials_oos_max_drawdown_not_nan",
        ).ddl_if(dialect="postgresql"),
    )
    op.create_index(
        "ix_strategy_trials_strategy_started",
        "strategy_trials",
        ["strategy_id", sa.text("started_at DESC")],
    )
    op.create_index(
        "ix_strategy_trials_hypothesis",
        "strategy_trials",
        ["hypothesis_id"],
    )
    # One-shot holdout seal (§4.2/§5.1): at most one holdout_confirmation trial
    # per strategy_id of ANY status, enforced at the DB level so a future bypass
    # of TrialRecorder (04-2) still cannot slip a second holdout run through.
    #
    # The predicate deliberately keys on run_type alone, NOT
    # `AND status='completed'`. Because TrialRecorder inserts the trial row
    # BEFORE dispatch (status='running'), and the run reads the sealed holdout
    # data during dispatch, a run that reads the holdout and then errors has
    # ALREADY consumed the one-and-only permitted look at that data -- so the
    # seal must trip on the first attempt, not only on success. §4.2 states the
    # seal trips when "no prior holdout_confirmation trial row exists" (any
    # status); an earlier completed-only predicate diverged from that and left
    # the failure/retry path able to re-read the sealed data. Accepted tradeoff:
    # a holdout attempt that errors (even before finishing) permanently consumes
    # the seal; re-running requires an operator append-only audit correction
    # (C3), never a silent retry -- intentional fail-closed behavior for
    # one-shot holdout data. Dual postgresql_where/sqlite_where so the
    # constraint is portably declared for both the real Postgres schema and
    # this repo's SQLite-backed model tests (strategy_registry/models.py
    # precedent).
    op.create_index(
        "uix_strategy_trials_one_holdout_confirmation",
        "strategy_trials",
        ["strategy_id"],
        unique=True,
        postgresql_where=sa.text("run_type = 'holdout_confirmation'"),
        sqlite_where=sa.text("run_type = 'holdout_confirmation'"),
    )

    # ── research_data_windows ────────────────────────────────────────────────
    # The train/OOS/holdout date partition itself (§4.2/§5.1). Per-strategy
    # scoping is the operator-confirmed default (design doc §8 Q1,
    # 2026-07-22); the nullable strategy_family column still supports a
    # per-family override via the scope-XOR CHECK below.
    op.create_table(
        "research_data_windows",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("strategy_family", sa.Text(), nullable=True),
        sa.Column("strategy_id", sa.Text(), nullable=True),
        sa.Column("train_start", sa.Date(), nullable=False),
        sa.Column("train_end", sa.Date(), nullable=False),
        sa.Column("oos_start", sa.Date(), nullable=False),
        sa.Column("oos_end", sa.Date(), nullable=False),
        sa.Column("holdout_start", sa.Date(), nullable=False),
        sa.Column("holdout_end", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_research_data_windows"),
        sa.CheckConstraint(
            "(strategy_family IS NULL) != (strategy_id IS NULL)",
            name="ck_research_data_windows_scope",
        ),
        sa.CheckConstraint(
            "train_start < train_end AND train_end <= oos_start AND "
            "oos_start < oos_end AND oos_end <= holdout_start AND "
            "holdout_start < holdout_end",
            name="ck_research_data_windows_order",
        ),
    )
    op.create_index(
        "ix_research_data_windows_strategy_id",
        "research_data_windows",
        ["strategy_id"],
    )
    op.create_index(
        "ix_research_data_windows_strategy_family",
        "research_data_windows",
        ["strategy_family"],
    )

    # ── promotion_decisions ──────────────────────────────────────────────────
    # Append-only audit record of one PromotionPipeline.run invocation (§4.4,
    # §5.1); cited by the future backtesting->validated transition (04-5).
    op.create_table(
        "promotion_decisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("strategy_id", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("n_trials_used", sa.Integer(), nullable=False),
        sa.Column("dsr_value", sa.Numeric(), nullable=True),
        sa.Column("funnel_passed", sa.Boolean(), nullable=False),
        sa.Column("sensitivity_verdict", sa.Text(), nullable=True),
        sa.Column("stress_verdict", sa.Text(), nullable=True),
        sa.Column("overall_passed", sa.Boolean(), nullable=False),
        sa.Column("mlflow_run_id", sa.Text(), nullable=True),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_promotion_decisions"),
        sa.ForeignKeyConstraint(
            ["strategy_id", "config_hash"],
            ["strategy_definitions.strategy_id", "strategy_definitions.config_hash"],
            name="fk_promotion_decisions_definition",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "n_trials_used >= 0",
            name="ck_promotion_decisions_n_trials_nonnegative",
        ),
        sa.CheckConstraint(
            "sensitivity_verdict IS NULL OR sensitivity_verdict IN "
            "('robust', 'curve_fit')",
            name="ck_promotion_decisions_sensitivity_verdict",
        ),
        sa.CheckConstraint(
            "stress_verdict IS NULL OR stress_verdict IN ('solid', 'fragile')",
            name="ck_promotion_decisions_stress_verdict",
        ),
        # NaN backstop (Postgres-only) -- same rationale as strategy_trials'
        # oos_sharpe/oos_max_drawdown above. 04-4 writes dsr_value from the
        # Deflated-Sharpe computation, which can produce NaN; dsr_value is
        # informational per §8 Q3 but must still never persist a NaN that a
        # reviewer could misread as a real (very negative) figure. NULL stays
        # allowed for "not computed". ddl_if(postgresql): SQLite coerces
        # float('nan') to NULL on storage and rejects the `::numeric` cast
        # syntax.
        sa.CheckConstraint(
            "dsr_value IS NULL OR dsr_value <> 'NaN'::numeric",
            name="ck_promotion_decisions_dsr_value_not_nan",
        ).ddl_if(dialect="postgresql"),
    )
    op.create_index(
        "ix_promotion_decisions_strategy_created",
        "promotion_decisions",
        ["strategy_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("promotion_decisions")
    op.drop_index(
        "ix_research_data_windows_strategy_family", table_name="research_data_windows"
    )
    op.drop_index(
        "ix_research_data_windows_strategy_id", table_name="research_data_windows"
    )
    op.drop_table("research_data_windows")
    op.drop_index(
        "uix_strategy_trials_one_holdout_confirmation", table_name="strategy_trials"
    )
    op.drop_index("ix_strategy_trials_hypothesis", table_name="strategy_trials")
    op.drop_index("ix_strategy_trials_strategy_started", table_name="strategy_trials")
    op.drop_table("strategy_trials")
    op.drop_index("ix_strategy_hypotheses_strategy_id", table_name="strategy_hypotheses")
    op.drop_table("strategy_hypotheses")
