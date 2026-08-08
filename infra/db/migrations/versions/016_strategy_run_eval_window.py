"""Persist the effective evaluation window on every strategy_runs row.

Roadmap Gate 04, slice 04-4W (Phase W3), extending the 04-4W / migration 015
fix (``strategy_trials.eval_start_date``/``eval_end_date``,
docs/plans/04-identity-evaluation-context-design.md) to the
``strategy_runs`` recording path. ``StrategyRun`` (``strategy_registry/
models.py``) carries ``data_version`` but no evaluation window; two
``backtest``/``walk_forward`` runs over different windows, recorded under
the same ``strategy_id``/``config_hash``/``data_version``, would otherwise be
indistinguishable and unreconstructable now that
docs/plans/04-identity-evaluation-context-design.md moved
``backtest.start_date``/``backtest.end_date`` out of ``config_hash`` and into
"evaluation context." This migration adds the two columns that close that
gap for ``strategy_runs``, mirroring migration 015's approach exactly.

``eval_start_date``/``eval_end_date`` are added NULLABLE, mirroring
migration 015's ``strategy_trials`` precedent: existing rows predate this
column and have no value to backfill from without re-deriving it from
historical config snapshots, which is out of scope here. Nullability is NOT
relaxed for new rows at the DB layer (no NOT NULL CHECK is added) -- the
writer (``strategy_registry.registry.StrategyRegistry.record_run``) is the
enforcement point, requiring non-NULL, non-reversed dates exactly for the
run types that require ``data_version`` (``backtest``/``walk_forward``, the
same ``_REQUIRE_DATA_VERSION`` set used for the existing C7 data_version
gate), and leaving them optional for ``unit``/``signal_ic``/``paper``/
``live``.

No CHECK-constraint change: ``ck_strategy_runs_run_type``/
``ck_strategy_runs_status`` are untouched -- this migration only adds
descriptive DATE columns.

Revision ID: 016
Revises: 015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: str = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # C2: Alembic op.add_column only, never a raw ALTER TABLE.
    op.add_column(
        "strategy_runs",
        sa.Column("eval_start_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "strategy_runs",
        sa.Column("eval_end_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_runs", "eval_end_date")
    op.drop_column("strategy_runs", "eval_start_date")
