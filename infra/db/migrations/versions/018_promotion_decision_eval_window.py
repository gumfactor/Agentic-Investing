"""Persist the effective evaluation window on every promotion_decisions row.

Roadmap Gate 04, slice 04-4W, PR #50 Codex round-1 finding R1-A (P1).
``strategy_trials`` (migration 016) and ``strategy_runs`` (migration 017)
already record the effective evaluation window; ``promotion_decisions`` --
the AUTHORITATIVE record of a promotion verdict -- did not, even though it
is exactly as susceptible to the class this slice exists to close: once
``config_hash`` excludes ``backtest.start_date``/``backtest.end_date``
(a000e87, docs/plans/04-identity-evaluation-context-design.md), the SAME
strategy/config/data-snapshot can legally be promoted more than once over
DIFFERENT evaluation windows -- and without this column, the resulting
``promotion_decisions`` rows would be indistinguishable by interval:
``evidence_json`` records only ``data_version``, not which window was
actually promoted.

This slipped past the class-closing invariant test
(``backtesting/tests/test_eval_window_invariant.py``) because check 1's
discovery predicate keyed on a ``data_version`` column alone, and
``PromotionDecision`` carries no ``data_version`` column of its own (only
``config_hash``, ``strategy_id``). The predicate is widened in the same PR
to ``data_version`` OR ``config_hash`` -- see that test module's docstring
for the full allowlist this widening pulls in.

``eval_start_date``/``eval_end_date`` are added NULLABLE, mirroring
migrations 016/017's precedent: existing rows predate this column and have
no value to backfill from without re-deriving it from historical
``evidence_json``/config snapshots, which is out of scope here. Nullability
is NOT relaxed for new rows at the DB layer (no NOT NULL CHECK is added) --
the writer (``backtesting.validation.promotion_pipeline.PromotionPipeline.
_persist_decision``) is the enforcement point, sourcing the value directly
from the ``eval_window`` already threaded into ``PromotionPipeline.run()``
(never re-derived from ``StrategyDefinition.config``).

No CHECK-constraint change: ``ck_promotion_decisions_n_trials_nonnegative``/
``ck_promotion_decisions_sensitivity_verdict``/
``ck_promotion_decisions_stress_verdict`` are untouched -- this migration
only adds descriptive DATE columns.

04-5's ``validated``-status migration therefore shifts to 019 (previously
018, before this finding).

Revision ID: 018
Revises: 017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: str = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # C2: Alembic op.add_column only, never a raw ALTER TABLE.
    op.add_column(
        "promotion_decisions",
        sa.Column("eval_start_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "promotion_decisions",
        sa.Column("eval_end_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("promotion_decisions", "eval_end_date")
    op.drop_column("promotion_decisions", "eval_start_date")
