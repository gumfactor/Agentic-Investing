"""Add provisional marker to signal_ic_stats.

BUG-008 / 01B-2 adversarial-review fix: rows persisted from
``--provisional-no-universe`` runs were byte-identical to PIT-enforced runs,
so a reader could not tell certified research from provisional research.

This is a deliberately minimal INTERIM marker. 01B-3 implements the full
versioned research identity of docs/plans/01b-research-validity-design.md §4
(``research_methodologies`` / ``research_runs`` records and a
``research_run_id`` foreign key on ``signal_ic_stats``, ``factor_scores``,
and ``alpha_scores``), which supersedes this boolean; do not extend it —
extend §4 instead.

Existing rows default to ``provisional = TRUE``: every row written before
01B-2 was computed under the current-membership universe and IS provisional
per the design plan ("Historical outputs produced with the current-membership
universe ... remain provisional").

Revision ID: 010
Revises: 009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: str = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "signal_ic_stats",
        sa.Column(
            "provisional",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
    )


def downgrade() -> None:
    op.drop_column("signal_ic_stats", "provisional")
