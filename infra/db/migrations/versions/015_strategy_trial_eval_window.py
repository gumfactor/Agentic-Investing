"""Persist the effective evaluation window on every strategy_trials row.

Roadmap Gate 04, slice 04-4W (docs/plans/04-4W-evaluation-window-threading-scope.md
§2 W1), scoped after PR #49 Codex round-3 surfaced that the evaluation window
computed at ``TrialRecorder._effective_range(config)`` (``trial_recorder.py``)
was used for the §4.2 holdout guard and then discarded rather than persisted
on the ``strategy_trials`` row it gates. Once
``docs/plans/04-identity-evaluation-context-design.md`` (operator decision,
2026-08-07, Option 1) moved ``backtest.start_date``/``backtest.end_date`` out
of ``config_hash`` and into "evaluation context," the window is no longer
reconstructable from the frozen ``config_hash`` alone -- a train_oos
measurement and a holdout measurement recorded under the SAME ``config_hash``
became indistinguishable from the row alone. This migration adds the two
columns that close that gap.

``eval_start_date``/``eval_end_date`` are added NULLABLE, mirroring the
documented ``hypothesis_id`` nullable-for-legacy-only precedent on
``StrategyTrial`` (``strategy_registry/selection_models.py``): existing rows
predate this column and have no value to backfill from without re-deriving
it from historical ``metrics_json``/config snapshots, which is out of scope
here. Nullability is NOT relaxed for new rows at the DB layer (no NOT NULL
CHECK is added) -- the writer (``backtesting.validation.trial_recorder.
TrialRecorder._run_and_record``) is the enforcement point that emits a
non-NULL value for every row it inserts going forward, the same
application-layer-enforced-nullability discipline this schema already uses
for ``hypothesis_id``.

No CHECK-constraint change: ``ck_strategy_trials_window`` /
``ck_strategy_trials_run_type`` /
``ck_strategy_trials_holdout_window_iff_confirmation`` are untouched --
this migration only adds descriptive DATE columns, it does not change which
combinations of window/run_type/eval dates are permitted.

**Chain-order note**: chains onto 017 (``strategy_definitions.
fingerprint_algo_version``, Phase W1), not 014 directly -- see 017's
docstring for why the true revision chain (``014 -> 017 -> 015 -> 016``)
does not match filename sort order.

Revision ID: 015
Revises: 017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: str = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # C2: Alembic op.add_column only, never a raw ALTER TABLE.
    op.add_column(
        "strategy_trials",
        sa.Column("eval_start_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "strategy_trials",
        sa.Column("eval_end_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_trials", "eval_end_date")
    op.drop_column("strategy_trials", "eval_start_date")
