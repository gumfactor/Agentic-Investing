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

``fingerprint_algo_version`` is added NOT NULL with ``server_default='1'`` --
unlike migration 015/016's nullable eval-window columns, every EXISTING row
unambiguously WAS computed under algorithm version 1 (the only algorithm
that has ever existed until ``a000e87``), so there is no legitimate "unknown"
value to leave nullable for. The server-side default backfills every
pre-migration row to 1 in the same DDL statement (C2: op.add_column with
server_default, no raw ALTER TABLE, no separate data-migration UPDATE
statement). The application (``strategy_registry.registry.StrategyRegistry.
add_definition``/``register``) writes the current
``strategy_registry.fingerprint.FINGERPRINT_ALGO_VERSION`` (2) explicitly for
every new row going forward -- this migration does not rely on the column
default for new inserts, only for backfilling history.

No existing hash is rewritten by this migration -- it is schema-only,
exactly like migration 015/016. Recomputing/migrating old config_hash values
themselves remains explicitly out of scope (the operator's waiver), and this
migration does not attempt it.

**Chain-order note**: this file is numbered 017 (per the slice's final
numbering: 015=strategy_trials eval window, 016=strategy_runs eval window,
017=this fingerprint version marker -- 04-5's ``validated`` status migration
shifts to 018), but it was AUTHORED FIRST within slice 04-4W (Phase W1,
identity/version marker) and chains directly onto 014 (``down_revision =
"014"``) rather than onto 016. Migrations 015 and 016 (Phases W2/W3) chain
AFTER this one (015's ``down_revision = "017"``; 016's ``down_revision =
"015"``), so the true Alembic revision chain is
``014 -> 017 -> 015 -> 016``, even though the filenames sort
``015, 016, 017``. This keeps every phase's commit independently valid (no
migration ever references a down_revision that does not yet exist in that
commit) while preserving the requested final file-numbering scheme.

Revision ID: 017
Revises: 014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "017"
down_revision: str = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # C2: Alembic op.add_column only, never a raw ALTER TABLE. server_default
    # backfills every existing row to '1' (the only algorithm that has ever
    # existed prior to this migration) in the same statement.
    op.add_column(
        "strategy_definitions",
        sa.Column(
            "fingerprint_algo_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("strategy_definitions", "fingerprint_algo_version")
