"""Point-in-time universe eligibility attributes: universe_eligibility_batches,
universe_eligibility_attributes.

Roadmap 03A-4a (docs/plans/03a-immutable-research-data-design.md §1). Adds
the second, independent PIT axis alongside ``universe_membership``
(migration 009): a ticker can be a member on a date and still fail a
strategy-declared eligibility filter (ADV, price, security type) on that same
date. §1.1 explains why this is a separate table family rather than growing
``universe_membership``'s scope.

``market_cap_usd`` is deliberately NOT populated by this migration (operator
decision, 03A-4a task brief): yfinance has no filing-dated
(``known_at``/``source_data_asof``-comparable) shares-outstanding source, so
no PIT market-cap series can be certified yet (§1.4). The schema's
``attribute_name`` column is free text specifically so ``market_cap_usd`` can
be added later without a new migration once a dated fundamentals source
exists; until then, strategy configs that declare a market-cap filter must
fail closed at config-load time (see ``data/universe/eligibility_config.py``),
never silently pass every ticker.

This migration ships schema only. The daily batch job that populates
``adv_usd_20d``/``price_usd`` from ``daily_prices``, and the hand-curated
``security_type`` historical backfill, are Phase B (03A-4b) — a separate
slice per the task brief's scope boundary.

Revision ID: 013
Revises: 012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: str = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # btree_gist already enabled by migration 009 for universe_membership's
    # EXCLUDE constraint; re-assert defensively (idempotent, cheap) in case
    # this migration is ever applied to a DB where 009 was applied before
    # the extension existed.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # ── universe_eligibility_batches ────────────────────────────────────────
    # One row per nightly/backfill eligibility-attribute computation run.
    # Mirrors universe_import_batches' provenance role (§1.2 computation_batch_id)
    # but is deliberately simpler: no staged/validated/published gate, because
    # eligibility attributes are append-only per-batch facts, not a single
    # membership set that must be atomically "the current truth" before use
    # (a caller always resolves eligibility as of a date against whichever
    # batch's row covers that date, not "the latest batch" as a whole).
    # Correcting a bad computation publishes a NEW batch rather than mutating
    # rows in place (C3-style discipline, §1.2).
    op.create_table(
        "universe_eligibility_batches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("universe_id", sa.Text(), nullable=False),
        sa.Column("code_version", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("n_attribute_rows", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "length(universe_id) > 0 AND length(code_version) > 0",
            name="ck_universe_eligibility_batches_nonempty_ids",
        ),
    )
    op.create_index(
        "ix_universe_eligibility_batches_universe_computed",
        "universe_eligibility_batches",
        ["universe_id", "computed_at"],
    )

    # ── universe_eligibility_attributes ─────────────────────────────────────
    # Append-only, effective-dated fact table (§1.2). One row per
    # (universe_id, ticker, attribute_name, effective_start).
    op.create_table(
        "universe_eligibility_attributes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("universe_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        # Free text, not an enum, so a new attribute (e.g. market_cap_usd
        # once a dated fundamentals source exists) never requires a schema
        # migration -- matches the config-driven filter design (CLAUDE.md).
        sa.Column("attribute_name", sa.Text(), nullable=False),
        sa.Column("attribute_value_numeric", sa.Numeric(18, 6), nullable=True),
        sa.Column("attribute_value_text", sa.Text(), nullable=True),
        sa.Column("effective_start", sa.Date(), nullable=False),
        sa.Column("effective_end", sa.Date(), nullable=True),
        sa.Column("computed_from", sa.Text(), nullable=False),
        sa.Column("source_data_asof", sa.Date(), nullable=False),
        sa.Column("computation_batch_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["computation_batch_id"],
            ["universe_eligibility_batches.id"],
            name="fk_universe_eligibility_attributes_batch",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(universe_id) > 0 AND length(ticker) > 0 AND length(attribute_name) > 0",
            name="ck_universe_eligibility_attributes_nonempty_ids",
        ),
        sa.CheckConstraint(
            "effective_end IS NULL OR effective_end > effective_start",
            name="ck_universe_eligibility_attributes_valid_range",
        ),
        # Exactly one of numeric/text is populated (§1.2 row 4) -- numeric
        # attributes (adv_usd_20d, price_usd, ...) use the numeric column,
        # security_type uses the text column, never both, never neither.
        sa.CheckConstraint(
            "(attribute_value_numeric IS NOT NULL AND attribute_value_text IS NULL) "
            "OR (attribute_value_numeric IS NULL AND attribute_value_text IS NOT NULL)",
            name="ck_universe_eligibility_attributes_exactly_one_value",
        ),
        # Future-leak guard (§1.5 acceptance test 4): a computation using
        # input data dated after the interval it claims to describe is a
        # defect, rejected at ingestion, not merely documented.
        sa.CheckConstraint(
            "source_data_asof <= effective_start",
            name="ck_universe_eligibility_attributes_source_not_future",
        ),
    )
    op.create_index(
        "ix_universe_eligibility_attributes_universe_ticker_attr",
        "universe_eligibility_attributes",
        ["universe_id", "ticker", "attribute_name"],
    )
    op.create_index(
        "ix_universe_eligibility_attributes_universe_attr_start",
        "universe_eligibility_attributes",
        ["universe_id", "attribute_name", "effective_start"],
    )
    # No overlapping intervals for a given (universe_id, ticker, attribute_name)
    # WITHIN one computation batch -- mirrors migration 009's
    # import_batch_id-scoped EXCLUDE constraint (§1.2: "a per-(universe_id,
    # ticker, attribute_name, computation_batch_id) no-overlap EXCLUDE
    # constraint mirrors migration 009's batch-scoped pattern"). No global
    # no-overlap constraint is imposed ACROSS attribute_name, since different
    # attributes are independent series (§1.2).
    op.execute(
        """
        ALTER TABLE universe_eligibility_attributes
        ADD CONSTRAINT ex_universe_eligibility_attributes_no_overlap
        EXCLUDE USING gist (
            universe_id WITH =,
            computation_batch_id WITH =,
            ticker WITH =,
            attribute_name WITH =,
            daterange(effective_start, effective_end, '[)') WITH &&
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE universe_eligibility_attributes "
        "DROP CONSTRAINT IF EXISTS ex_universe_eligibility_attributes_no_overlap"
    )
    op.drop_table("universe_eligibility_attributes")
    op.drop_table("universe_eligibility_batches")
