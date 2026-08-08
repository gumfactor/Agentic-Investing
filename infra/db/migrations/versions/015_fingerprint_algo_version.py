"""Persist the fingerprint algorithm version on every strategy_definitions row.

Roadmap Gate 04, slice 04-4W (docs/plans/04-identity-evaluation-context-
design.md, operator decision 2026-08-07, Option 1). ``a000e87`` changed
``strategy_registry.fingerprint``'s canonical hash input to exclude
``backtest.start_date``/``backtest.end_date`` -- config_hash values computed
before that change (under the old, dates-included algorithm) and values
computed after it (under the new, dates-excluded algorithm) are members of
two DIFFERENT hash spaces: the same YAML content can hash differently
depending on which algorithm produced it.

The operator explicitly waived migrating/recomputing existing data (nothing
is live yet; C8 qualification has not started), but that waiver previously
lived only in a design doc -- undiscoverable from the schema itself, and
liable to keep resurfacing as a "how do we know which algorithm this hash
used?" finding on every future review of ``verify_config_integrity()`` or
any cross-hash comparison. This migration makes the waiver moot rather than
merely declared: every row now carries the algorithm version it was actually
computed under, so a pre-v2 row is DISTINGUISHABLE from a v2 row, and any
future recompute (should this project ever approach live capital) is a
straightforward filtered UPDATE rather than an undocumented assumption.

``fingerprint_algo_version`` is added NOT NULL. Every EXISTING row
unambiguously WAS computed under algorithm version 1 (the only algorithm
that has ever existed until ``a000e87``), so there is no legitimate "unknown"
value to leave nullable for. On Postgres, the column is added with a
TEMPORARY ``server_default='1'`` so ``op.add_column`` can backfill every
pre-existing row to 1 in one DDL statement without a separate UPDATE (C2:
op.add_column only, never a raw ALTER TABLE) -- then, in the SAME
migration, that server default is immediately DROPPED
(``op.alter_column(..., server_default=None)``); the column stays NOT
NULL. SQLite requires a different sequence -- see the R1-C note below.

**PM amendment A3 (2026-08-08, P1) -- corrects an earlier, factually wrong
claim in this docstring**: an earlier version of this migration kept the
Postgres ``server_default='1'`` permanently and asserted that was safe
because "the application... writes the current FINGERPRINT_ALGO_VERSION
explicitly for every new row." That claim was WRONG -- a Postgres
``server_default`` applies to every FUTURE INSERT that omits the column,
not only to this migration's one-time backfill. Left in place, it would
silently stamp v1 on any row inserted by a future code path, a direct ORM
construction, or a raw SQL insert that omits the column -- mislabelling a
row current code just created under the CURRENT algorithm as a pre-v2
legacy row, and then have
``strategy_registry.registry.require_current_fingerprint_algo_version``
correctly-but-wrongly refuse it, sending an operator to "re-register a
strategy" that was never stale. That is a worse failure than the one this
column exists to prevent: it manufactures the exact condition it
diagnoses.

Dropping the server default after the one-time backfill closes that hole:
the ORM-side default now lives in Python
(``strategy_registry.models.StrategyDefinition.fingerprint_algo_version``'s
``default=FINGERPRINT_ALGO_VERSION``, always the CURRENT algorithm version,
never a driftable hard-coded literal) as the single supported path for an
omitted field, and a raw SQL insert that bypasses the ORM and omits this
column now fails NOT NULL instead of silently minting a mislabelled row.

**R1-C (PR #50 Codex round-1 P2) -- corrects an earlier, factually wrong
SQLite-dialect claim.** The version of this migration reviewed for A3
skipped the server-default drop entirely on SQLite, reasoning "SQLite has
no persistent server-side default to drop." **That reasoning was false.**
SQLite's ``ALTER TABLE ... ADD COLUMN ... DEFAULT 1`` stores that default
PERMANENTLY in the table definition (unlike a transient statement-time
value) -- every future ``INSERT`` that omits the column keeps receiving
``1`` from SQLite itself, which reintroduces the EXACT A3 mislabelling bug
on SQLite: a fresh v2 row inserted by any writer that omits the column
(bypassing the ORM's Python-side default, e.g. a raw SQL insert) would
still be silently stamped v1 by the database, not merely left NULL. The
fail-closed guarantee A3 exists to provide was therefore untrue on SQLite
specifically.

SQLite's ``ALTER TABLE`` cannot add/drop/alter a column default at all
(there is no ``ALTER TABLE ... ALTER COLUMN`` in SQLite), so the Postgres
"add-with-default-then-drop-default" sequence has no SQLite equivalent.
The correct SQLite sequence instead is: add the column NULLABLE with NO
default at all; backfill existing rows with an explicit ``UPDATE``; then
use ``op.batch_alter_table`` (which transparently rebuilds the table under
SQLite, the standard Alembic mechanism for column alterations SQLite's
``ALTER TABLE`` cannot express directly) to flip the column to NOT NULL.
The end state is identical to Postgres on both dialects: NOT NULL, NO
persistent default anywhere -- verified by asserting on a live SQLite
connection that a raw INSERT omitting the column fails (rather than
silently receiving 1) after this migration runs.

No existing hash is rewritten by this migration -- it is schema-only,
exactly like migration 016/017. Recomputing/migrating old config_hash values
themselves remains explicitly out of scope (the operator's waiver), and this
migration does not attempt it.

Revision ID: 015
Revises: 014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: str = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # R1-C: SQLite's ALTER TABLE cannot add/drop/alter a column
        # default, and an ADD COLUMN ... DEFAULT clause is PERMANENT (not a
        # one-time backfill value like Postgres's server_default combined
        # with a later DROP DEFAULT) -- so the Postgres sequence below
        # cannot be used here at all. Add the column with NO default
        # (nullable, so the ADD COLUMN itself cannot fail on existing
        # rows), backfill explicitly, then use batch_alter_table (rebuilds
        # the table under the hood -- the standard Alembic mechanism for a
        # SQLite column alteration ALTER TABLE cannot express directly) to
        # flip it NOT NULL. End state: NOT NULL, no persistent default --
        # identical to the Postgres branch below.
        op.add_column(
            "strategy_definitions",
            sa.Column("fingerprint_algo_version", sa.Integer(), nullable=True),
        )
        op.execute(
            "UPDATE strategy_definitions SET fingerprint_algo_version = 1 "
            "WHERE fingerprint_algo_version IS NULL"
        )
        with op.batch_alter_table("strategy_definitions") as batch_op:
            batch_op.alter_column(
                "fingerprint_algo_version",
                existing_type=sa.Integer(),
                nullable=False,
            )
    else:
        # C2: Alembic op.add_column only, never a raw ALTER TABLE. The
        # temporary server_default backfills every existing row to '1' (the
        # only algorithm that has ever existed prior to this migration) in
        # this one statement.
        op.add_column(
            "strategy_definitions",
            sa.Column(
                "fingerprint_algo_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )
        # PM amendment A3: drop the server default immediately after using
        # it to backfill pre-existing rows above. A Postgres server_default
        # applies to every FUTURE insert that omits the column too, not
        # only to this one-time backfill -- leaving it in place would
        # silently mislabel v1 any row a future writer inserts without
        # explicitly setting this column. The column stays NOT NULL; the
        # only remaining "default" is the ORM-side
        # strategy_registry.models.StrategyDefinition.fingerprint_algo_version
        # Python default (always the current algorithm version), and any
        # non-ORM writer that omits the column now fails NOT NULL instead
        # of being silently mislabelled.
        op.alter_column("strategy_definitions", "fingerprint_algo_version", server_default=None)


def downgrade() -> None:
    # op.drop_column works natively on both dialects (modern SQLite
    # supports DROP COLUMN directly; no batch mode needed for a drop) --
    # verified via a live round-trip on both.
    op.drop_column("strategy_definitions", "fingerprint_algo_version")
