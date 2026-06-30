"""Add quantity_overrides JSONB column to blotter_approvals.

Revision ID: 006
Revises: 005
Create Date: 2026-06-29

Stores operator quantity reductions from the Streamlit dashboard
blotter approval UI (M5.8 Sprint 1). Only orders where the operator
changed the quantity are present; orders with no change are absent.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "006"
down_revision: str = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "blotter_approvals",
        sa.Column("quantity_overrides", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("blotter_approvals", "quantity_overrides")
