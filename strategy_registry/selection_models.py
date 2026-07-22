"""ORM models for the strategy-selection protocol (Gate 04 slice 04-1,
docs/plans/04-strategy-selection-protocol-design.md §5.1).

Mirrors ``infra/db/migrations/versions/014_strategy_selection_protocol.py``.
The Alembic migration is the canonical Postgres schema (C2); this module is
used directly by application code and by tests (via
``Base.metadata.create_all`` against SQLite), following the same manual-sync
discipline as ``data/research/models.py`` mirroring
``012_research_identity.py``.

Shares the declarative ``Base`` from ``strategy_registry.models`` (not a new
one) because ``strategy_trials.config_hash`` and
``promotion_decisions.config_hash`` are composite foreign keys against
``strategy_definitions(strategy_id, config_hash)`` -- both tables must live
in the same ``MetaData`` for ``Base.metadata.create_all()`` to resolve the FK
and for SQLAlchemy relationship/test wiring to work without a second,
disconnected registry.

Pure schema module (04-1 scope): no ``TrialRecorder``, no
``PromotionPipeline``, no ``validated`` status wiring here -- those are
04-2..04-5.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from strategy_registry.models import Base


class StrategyHypothesis(Base):
    """Pre-registered research question, written before any candidate config
    is run (§4.1/§5.1). No FK to ``strategy_definitions``/``strategies`` --
    a hypothesis can precede either.
    """

    __tablename__ = "strategy_hypotheses"
    __table_args__ = (
        CheckConstraint(
            "length(strategy_id) > 0",
            name="ck_strategy_hypotheses_strategy_id_nonempty",
        ),
        Index("ix_strategy_hypotheses_strategy_id", "strategy_id"),
        # Composite unique on (id, strategy_id) -- id is already the PK so this
        # is trivially satisfied; it exists solely as the FK target of
        # StrategyTrial's composite (hypothesis_id, strategy_id) FK, so a trial
        # can only cite a hypothesis pre-registered for its OWN strategy_id.
        UniqueConstraint(
            "id", "strategy_id", name="uq_strategy_hypotheses_id_strategy_id"
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        autoincrement=True,
        primary_key=True,
    )
    strategy_id: Mapped[str] = mapped_column(Text, nullable=False)
    hypothesis_text: Mapped[str] = mapped_column(Text, nullable=False)
    param_grid_json: Mapped[Optional[dict[str, Any]]] = mapped_column(sa.JSON)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    # Set on the first linked strategy_trials row; param_grid_json becomes
    # immutable at the application layer once non-null (enforced by the
    # future TrialRecorder, not a DB trigger -- matches
    # verify_config_integrity()'s existing application-layer-enforcement
    # style).
    frozen_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<StrategyHypothesis id={self.id} strategy_id={self.strategy_id!r}>"


class StrategyTrial(Base):
    """Append-only (C3-style) record of one candidate run attempt (§4.1/§5.1).

    A row is inserted before the wrapped instrument dispatches (04-2's
    ``TrialRecorder``) so a crashed/discarded run still counts toward
    ``n_trials``.
    """

    __tablename__ = "strategy_trials"
    __table_args__ = (
        ForeignKeyConstraint(
            ["strategy_id", "config_hash"],
            ["strategy_definitions.strategy_id", "strategy_definitions.config_hash"],
            name="fk_strategy_trials_definition",
            ondelete="RESTRICT",
        ),
        # Composite FK: (hypothesis_id, strategy_id) must match a single
        # strategy_hypotheses (id, strategy_id) row -- a trial can only cite a
        # hypothesis pre-registered for its OWN strategy_id. hypothesis_id
        # stays nullable; under MATCH SIMPLE a NULL hypothesis_id skips FK
        # enforcement, preserving the legacy-backfill path.
        ForeignKeyConstraint(
            ["hypothesis_id", "strategy_id"],
            ["strategy_hypotheses.id", "strategy_hypotheses.strategy_id"],
            name="fk_strategy_trials_hypothesis",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            '"window" IN (\'train_oos\', \'holdout\')',
            name="ck_strategy_trials_window",
        ),
        CheckConstraint(
            "run_type IN ('walk_forward', 'parameter_sweep_variant', "
            "'holdout_confirmation')",
            name="ck_strategy_trials_run_type",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'errored')",
            name="ck_strategy_trials_status",
        ),
        # Couple window and run_type: a row touches the holdout window IFF it is
        # a holdout_confirmation run. Prevents a holdout-window row hiding under
        # run_type='walk_forward'/'parameter_sweep_variant', which the
        # run_type-keyed one-shot seal would not catch. `"window"` is quoted
        # (reserved keyword). Boolean-equality form is portable to SQLite.
        CheckConstraint(
            "(\"window\" = 'holdout') = (run_type = 'holdout_confirmation')",
            name="ck_strategy_trials_holdout_window_iff_confirmation",
        ),
        # NaN backstop (Postgres-only). Postgres `numeric` DOES support NaN
        # (contrary to a now-corrected claim in design doc §5.1): an
        # unfiltered float('nan')/numpy.nan written by 04-2's TrialRecorder
        # would persist and poison downstream DSR/funnel/stress comparisons.
        # `col <> 'NaN'::numeric` is FALSE for a NaN (CHECK fails, NaN
        # rejected) and TRUE for any real value; NULL stays allowed for the
        # "not computed yet" case (status='running'). The `col = col`
        # self-comparison idiom does NOT work: Postgres treats `NaN = NaN` as
        # TRUE and would pass NaN through.
        #
        # ddl_if(postgresql) is a deliberate, documented dialect divergence:
        # the `::numeric` cast is a syntax error on SQLite, and SQLite coerces
        # float('nan') to NULL at storage time (verified), so NaN cannot
        # poison the SQLite test path -- there is nothing for a SQLite CHECK
        # to reject. App-layer commitment: 04-2/04-4 writers must still
        # normalize any non-finite float to None before insert, so behavior
        # is identical (NaN -> NULL/rejected) regardless of backend.
        CheckConstraint(
            "oos_sharpe IS NULL OR oos_sharpe <> 'NaN'::numeric",
            name="ck_strategy_trials_oos_sharpe_not_nan",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "oos_max_drawdown IS NULL OR oos_max_drawdown <> 'NaN'::numeric",
            name="ck_strategy_trials_oos_max_drawdown_not_nan",
        ).ddl_if(dialect="postgresql"),
        # started_at DESC mirrors the migration's
        # sa.text("started_at DESC") ordering exactly (recent-trials-first
        # lookups); a bare ascending column would silently diverge from the
        # canonical Postgres schema.
        Index(
            "ix_strategy_trials_strategy_started",
            "strategy_id",
            sa.text("started_at DESC"),
        ),
        Index("ix_strategy_trials_hypothesis", "hypothesis_id"),
        # One-shot holdout seal (§4.2): at most one holdout_confirmation trial
        # per strategy_id of ANY status. Keyed on run_type alone, NOT
        # `AND status='completed'`: TrialRecorder (04-2) inserts the row before
        # dispatch and the run reads the sealed holdout data during dispatch,
        # so a run that reads the holdout and then errors has already consumed
        # its single permitted look -- the seal must trip on the first attempt,
        # matching §4.2 ("no prior holdout_confirmation trial row exists"). See
        # the migration's fuller comment on the accepted fail-closed tradeoff.
        # Dual postgresql_where/sqlite_where so Base.metadata.create_all()
        # builds a working partial-unique index under both backends (mirrors
        # strategy_registry/models.py's uix_strategies_one_paper/one_live).
        Index(
            "uix_strategy_trials_one_holdout_confirmation",
            "strategy_id",
            unique=True,
            postgresql_where=sa.text("run_type = 'holdout_confirmation'"),
            sqlite_where=sa.text("run_type = 'holdout_confirmation'"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        autoincrement=True,
        primary_key=True,
    )
    strategy_id: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable only for a documented legacy-backfill migration path, never
    # for new rows (enforced by the future TrialRecorder, not the DB, to
    # allow a one-time backfill of pre-protocol trials without a
    # partial-NULL CHECK -- see design doc §5.1).
    hypothesis_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    window: Mapped[str] = mapped_column(Text, nullable=False)
    run_type: Mapped[str] = mapped_column(Text, nullable=False)
    data_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    oos_sharpe: Mapped[Optional[float]] = mapped_column(Numeric())
    oos_max_drawdown: Mapped[Optional[float]] = mapped_column(Numeric())
    metrics_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    mlflow_run_id: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<StrategyTrial id={self.id} strategy_id={self.strategy_id!r} "
            f"run_type={self.run_type!r} status={self.status!r}>"
        )


class ResearchDataWindow(Base):
    """The train/OOS/holdout date partition (§4.2/§5.1).

    Per-strategy scoping is the operator-confirmed default (design doc §8
    Q1, 2026-07-22, resolved PER-STRATEGY); the nullable
    ``strategy_family`` column still supports a per-family override via the
    scope-XOR CHECK below.
    """

    __tablename__ = "research_data_windows"
    __table_args__ = (
        CheckConstraint(
            "(strategy_family IS NULL) != (strategy_id IS NULL)",
            name="ck_research_data_windows_scope",
        ),
        CheckConstraint(
            "train_start < train_end AND train_end <= oos_start AND "
            "oos_start < oos_end AND oos_end <= holdout_start AND "
            "holdout_start < holdout_end",
            name="ck_research_data_windows_order",
        ),
        Index("ix_research_data_windows_strategy_id", "strategy_id"),
        Index("ix_research_data_windows_strategy_family", "strategy_family"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        autoincrement=True,
        primary_key=True,
    )
    strategy_family: Mapped[Optional[str]] = mapped_column(Text)
    strategy_id: Mapped[Optional[str]] = mapped_column(Text)
    train_start: Mapped[date] = mapped_column(Date, nullable=False)
    train_end: Mapped[date] = mapped_column(Date, nullable=False)
    oos_start: Mapped[date] = mapped_column(Date, nullable=False)
    oos_end: Mapped[date] = mapped_column(Date, nullable=False)
    holdout_start: Mapped[date] = mapped_column(Date, nullable=False)
    holdout_end: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        scope = self.strategy_id or self.strategy_family
        return f"<ResearchDataWindow id={self.id} scope={scope!r}>"


class PromotionDecision(Base):
    """Append-only audit record of one ``PromotionPipeline.run`` invocation
    (§4.4/§5.1); cited by the future ``backtesting -> validated`` transition
    (04-5).

    Per design doc §8 Q3 (resolved 2026-07-22): ``dsr_value`` is
    informational only -- no DSR floor is enforced by any CHECK constraint
    here; ``overall_passed`` is an independently-set boolean, never derived
    from ``dsr_value`` at the DB layer.
    """

    __tablename__ = "promotion_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["strategy_id", "config_hash"],
            ["strategy_definitions.strategy_id", "strategy_definitions.config_hash"],
            name="fk_promotion_decisions_definition",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "n_trials_used >= 0",
            name="ck_promotion_decisions_n_trials_nonnegative",
        ),
        CheckConstraint(
            "sensitivity_verdict IS NULL OR sensitivity_verdict IN "
            "('robust', 'curve_fit')",
            name="ck_promotion_decisions_sensitivity_verdict",
        ),
        CheckConstraint(
            "stress_verdict IS NULL OR stress_verdict IN ('solid', 'fragile')",
            name="ck_promotion_decisions_stress_verdict",
        ),
        # NaN backstop (Postgres-only) -- same rationale and dialect
        # divergence as StrategyTrial.oos_sharpe/oos_max_drawdown above.
        # dsr_value is informational per §8 Q3, but a persisted NaN could be
        # misread as a real (very negative) Deflated Sharpe.
        CheckConstraint(
            "dsr_value IS NULL OR dsr_value <> 'NaN'::numeric",
            name="ck_promotion_decisions_dsr_value_not_nan",
        ).ddl_if(dialect="postgresql"),
        # created_at DESC mirrors the migration's sa.text("created_at DESC").
        Index(
            "ix_promotion_decisions_strategy_created",
            "strategy_id",
            sa.text("created_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        autoincrement=True,
        primary_key=True,
    )
    strategy_id: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    n_trials_used: Mapped[int] = mapped_column(Integer, nullable=False)
    dsr_value: Mapped[Optional[float]] = mapped_column(Numeric())
    funnel_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sensitivity_verdict: Mapped[Optional[str]] = mapped_column(Text)
    stress_verdict: Mapped[Optional[str]] = mapped_column(Text)
    overall_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    mlflow_run_id: Mapped[Optional[str]] = mapped_column(Text)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<PromotionDecision id={self.id} strategy_id={self.strategy_id!r} "
            f"overall_passed={self.overall_passed}>"
        )
