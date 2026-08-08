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
value to leave nullable for. The column is added with a TEMPORARY
``server_default='1'`` so ``op.add_column`` can backfill every pre-existing
row to 1 in one DDL statement without a separate UPDATE (C2: op.add_column
only, never a raw ALTER TABLE) -- then, in the SAME migration, that server
default is immediately DROPPED (``op.alter_column(...,
server_default=None)``); the column stays NOT NULL.

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
    # C2: Alembic op.add_column only, never a raw ALTER TABLE. The temporary
    # server_default backfills every existing row to '1' (the only algorithm
    # that has ever existed prior to this migration) in this one statement.
    op.add_column(
        "strategy_definitions",
        sa.Column(
            "fingerprint_algo_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    # PM amendment A3: drop the server default immediately after using it to
    # backfill pre-existing rows above. A Postgres server_default applies to
    # every FUTURE insert that omits the column too, not just this one-time
    # backfill -- leaving it in place would silently mislabel v1 any row a
    # future writer inserts without explicitly setting this column. The
    # column stays NOT NULL; the only remaining "default" is the ORM-side
    # strategy_registry.models.StrategyDefinition.fingerprint_algo_version
    # Python default (always the current algorithm version), and any
    # non-ORM writer that omits the column now fails NOT NULL instead of
    # being silently mislabelled.
    #
    # F2 fix (adversarial review, 2026-08-08): dialect-gated -- SQLite's
    # ALTER TABLE does not support dropping/altering a column default at
    # all (op.alter_column(..., server_default=None) raises
    # "OperationalError: near ALTER: syntax error" on SQLite, reproduced).
    # This is not merely an "unsupported operation, skip it" workaround:
    # SQLite has no PERSISTENT server-side default to drop in the first
    # place in the sense Postgres does -- a SQLite column DEFAULT is a
    # table-definition-time clause, not a live catalog attribute a future
    # writer's INSERT silently inherits the way Postgres's server_default
    # does. The A3 guarantee this step exists to protect (no writer can
    # silently mint a mislabelled v1 row) is carried entirely by the
    # Python-side ORM default already on SQLite, so skipping this
    # Postgres-specific cleanup step there does not weaken it.
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("strategy_definitions", "fingerprint_algo_version", server_default=None)


def downgrade() -> None:
    op.drop_column("strategy_definitions", "fingerprint_algo_version")
