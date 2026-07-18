"""Corporate-action availability contract: known_at / announced_at / source_version.

BUG-009 (docs/plans/01b-research-validity-design.md §2.3). Corporate actions
are point-in-time inputs, not timeless adjustment factors: an action must not
be usable to adjust a score feature at time t unless it was *knowable* by t.
The existing ``corporate_actions`` table (migration 001) only records
``ex_date`` and ``ingested_at`` — neither is a defensible availability
timestamp (``ingested_at`` is an artifact of when *this system* happened to
pull the row, not when the action became publicly knowable).

This migration adds:

- ``announced_at`` (nullable): the source's actual announcement/knowledge
  timestamp, when available.
- ``known_at`` (NOT NULL after backfill): the availability timestamp actually
  used by the score/return cutoff logic. Derived as ``announced_at`` when
  present, otherwise the conservative date-only rule from
  ``data.universe.calendar.conservative_known_at_for_date_only_source``
  (no earlier than the close of the next trading session after ``ex_date``) —
  mirrors the same conservative-rule pattern already used for universe
  membership (migration 009 / design plan §1.1).
- ``source_version`` (NOT NULL after backfill, default ``'unknown'`` for
  legacy rows): the action-source version, so
  ``build_score_price_history_as_of`` / ``build_realized_total_return_as_of``
  can record exactly which action-source snapshot was used (§2.3).
- ``known_at_policy`` (NOT NULL after backfill): records which rule produced
  ``known_at`` — ``'source_announced'`` or ``'conservative_next_session'`` —
  for audit/debugging.

Existing rows (yfinance-sourced, date-only, no announcement timestamp) are
backfilled with the conservative next-session rule. yfinance's
``fetch_corporate_actions`` supplies no announcement date, so in practice
every row ingested by the current pipeline uses the conservative policy;
``data/storage/timescale_writer.py::upsert_corporate_actions`` now computes
and stores ``known_at``/``known_at_policy``/``source_version`` for new rows
using the same helper, so newly-ingested actions are usable by the cutoff-
aware builders without a second backfill pass.

Revision ID: 011
Revises: 010
"""

from __future__ import annotations

import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: str = "010"
branch_labels = None
depends_on = None

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def upgrade() -> None:
    op.add_column(
        "corporate_actions",
        sa.Column("announced_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "corporate_actions",
        sa.Column("known_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "corporate_actions",
        sa.Column("known_at_policy", sa.Text(), nullable=True),
    )
    op.add_column(
        "corporate_actions",
        sa.Column("source_version", sa.Text(), nullable=True),
    )

    # ── Backfill existing rows ──────────────────────────────────────────────
    # Every pre-011 row came from the date-only yfinance source with no
    # announced_at, so the conservative next-session rule applies uniformly.
    # Computed in Python (not SQL) to reuse the single conservative-rule
    # implementation rather than re-deriving NYSE holiday logic in SQL.
    from data.universe.calendar import conservative_known_at_for_date_only_source

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, ex_date FROM corporate_actions WHERE known_at IS NULL")
    ).fetchall()
    for row in rows:
        known_at = conservative_known_at_for_date_only_source(row.ex_date)
        connection.execute(
            sa.text(
                "UPDATE corporate_actions SET known_at = :known_at, "
                "known_at_policy = 'conservative_next_session', "
                "source_version = COALESCE(source_version, 'legacy_unknown') "
                "WHERE id = :id"
            ),
            {"known_at": known_at, "id": row.id},
        )

    op.alter_column("corporate_actions", "known_at", nullable=False)
    op.alter_column(
        "corporate_actions",
        "known_at_policy",
        nullable=False,
        server_default=sa.text("'conservative_next_session'"),
    )
    op.alter_column(
        "corporate_actions",
        "source_version",
        nullable=False,
        server_default=sa.text("'legacy_unknown'"),
    )
    op.create_check_constraint(
        "ck_corporate_actions_known_at_policy",
        "corporate_actions",
        "known_at_policy IN ('source_announced', 'conservative_next_session')",
    )
    op.create_index(
        "ix_corporate_actions_ticker_known_at",
        "corporate_actions",
        ["ticker", "known_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_corporate_actions_ticker_known_at", table_name="corporate_actions")
    op.drop_constraint(
        "ck_corporate_actions_known_at_policy", "corporate_actions", type_="check"
    )
    op.drop_column("corporate_actions", "source_version")
    op.drop_column("corporate_actions", "known_at_policy")
    op.drop_column("corporate_actions", "known_at")
    op.drop_column("corporate_actions", "announced_at")
