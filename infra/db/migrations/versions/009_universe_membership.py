"""Point-in-time universe membership: universe_membership, universe_symbol_history,
universe_import_batches.

BUG-008 (docs/plans/01b-research-validity-design.md §1). Introduces an
effective-dated constituent model so historical research/backtest callers can
stop assuming current S&P 500 membership was also historical membership.

Amended during the 01B-2 adversarial-review fix round (before any release or
non-scratch deployment of this revision, so amending in place rather than a
follow-up migration is safe and was the recorded choice): adds
``universe_import_batches.excluded_tickers`` — a JSON text audit record of
operator ``--exclude-tickers`` exclusions (tickers + reason) so exclusions
are DB-queryable rather than existing only in logs/docs. Second pre-release
amendment (Codex PR #34 review): the no-overlap EXCLUDE constraint is scoped
by ``import_batch_id`` so a coverage-advancing re-import (a complete new row
set per batch) does not collide with the previous published batch's rows.

Revision ID: 009
Revises: 008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: str = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # btree_gist is required for the EXCLUDE constraint below (range overlap
    # checks combined with equality checks on universe_id/ticker).
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # ── universe_import_batches ─────────────────────────────────────────────
    # One row per import run. Raw-source provenance (§1.2 step 1) plus the
    # publish gate (§1.2 step 5: publish only a complete validated import).
    op.create_table(
        "universe_import_batches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("universe_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column("raw_artifact_path", sa.Text(), nullable=False),
        sa.Column("raw_checksum_sha256", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="staged"),
        sa.Column("coverage_start", sa.Date(), nullable=True),
        sa.Column("coverage_end", sa.Date(), nullable=True),
        sa.Column("n_membership_rows", sa.Integer(), nullable=True),
        sa.Column("n_symbol_history_rows", sa.Integer(), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        # JSON text: {"tickers": [...], "reason": "..."} — operator
        # --exclude-tickers audit record (01B-2 fix round). NULL = none.
        sa.Column("excluded_tickers", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('staged', 'validated', 'published', 'rejected')",
            name="ck_universe_import_batches_status",
        ),
        sa.CheckConstraint(
            "coverage_end IS NULL OR coverage_start IS NULL OR coverage_end >= coverage_start",
            name="ck_universe_import_batches_coverage_range",
        ),
    )
    op.create_index(
        "ix_universe_import_batches_universe_status",
        "universe_import_batches",
        ["universe_id", "status"],
    )

    # ── universe_membership ─────────────────────────────────────────────────
    # Canonical effective-dated constituent model (§1.1).
    op.create_table(
        "universe_membership",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("universe_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("vendor_symbol", sa.Text(), nullable=True),
        sa.Column("effective_start", sa.Date(), nullable=False),
        sa.Column("effective_end", sa.Date(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=False),
        sa.Column("announced_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("known_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("import_batch_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["universe_import_batches.id"],
            name="fk_universe_membership_import_batch",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "effective_end IS NULL OR effective_end > effective_start",
            name="ck_universe_membership_valid_range",
        ),
        sa.CheckConstraint(
            "length(universe_id) > 0 AND length(ticker) > 0",
            name="ck_universe_membership_nonempty_ids",
        ),
    )
    op.create_index(
        "ix_universe_membership_universe_ticker",
        "universe_membership",
        ["universe_id", "ticker"],
    )
    op.create_index(
        "ix_universe_membership_universe_start",
        "universe_membership",
        ["universe_id", "effective_start"],
    )
    # No overlapping intervals for a given (universe_id, ticker) pair
    # WITHIN one import batch. Half-open [effective_start, effective_end)
    # matches daterange's default '[)' bounds; an open-ended row
    # (effective_end IS NULL) is represented with an unbounded upper end.
    #
    # Scoped by import_batch_id (Codex PR #34 P1 fix, amended pre-release):
    # each published batch is a complete self-consistent membership set and
    # PITUniverseLookup reads only the latest published batch, so a
    # re-import that advances coverage inserts a full new row set. An
    # unscoped constraint would reject every unchanged ticker's interval as
    # overlapping the previous batch's copy, making any second import fail
    # at the database layer.
    op.execute(
        """
        ALTER TABLE universe_membership
        ADD CONSTRAINT ex_universe_membership_no_overlap
        EXCLUDE USING gist (
            universe_id WITH =,
            import_batch_id WITH =,
            ticker WITH =,
            daterange(effective_start, effective_end, '[)') WITH &&
        )
        """
    )

    # ── universe_symbol_history ─────────────────────────────────────────────
    # Ticker-rename mapping (§1.1: "do not rewrite old ticks to the newest
    # ticker symbol"). daily_prices keeps rows under the vendor ticker that
    # was active on each date; this table lets a caller resolve continuity
    # across a rename without mutating price history.
    op.create_table(
        "universe_symbol_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("universe_id", sa.Text(), nullable=False),
        sa.Column("old_ticker", sa.Text(), nullable=False),
        sa.Column("new_ticker", sa.Text(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=False),
        sa.Column("known_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "universe_id", "old_ticker", "effective_date",
            name="uq_universe_symbol_history_change",
        ),
        sa.CheckConstraint(
            "old_ticker <> new_ticker",
            name="ck_universe_symbol_history_distinct_tickers",
        ),
    )
    op.create_index(
        "ix_universe_symbol_history_universe_old",
        "universe_symbol_history",
        ["universe_id", "old_ticker"],
    )
    op.create_index(
        "ix_universe_symbol_history_universe_new",
        "universe_symbol_history",
        ["universe_id", "new_ticker"],
    )


def downgrade() -> None:
    op.drop_table("universe_symbol_history")
    op.execute(
        "ALTER TABLE universe_membership DROP CONSTRAINT IF EXISTS ex_universe_membership_no_overlap"
    )
    op.drop_table("universe_membership")
    op.drop_table("universe_import_batches")
