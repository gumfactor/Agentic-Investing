"""blotter_approvals: operator approval gate for paper-trading blotter artifacts.

Revision ID: 005
Revises: 004
Create Date: 2026-06-25

Implements the approval gate table consumed by the daily_paper_trading Airflow
DAG (M5.4). The BlotterApprovalSensor polls this table waiting for a row
matching the current run's blotter_run_id before permitting order submission.

Safety rule C1: orders are never submitted without a row in this table
confirming operator review and explicit SHA-256 match of the reviewed artifact.
Safety rule C3: this table is append-only. Corrections use a future
correction_of column referencing the original blotter_run_id — never UPDATE
or DELETE rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "005"
down_revision: str = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blotter_approvals",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "blotter_run_id",
            sa.Text(),
            nullable=False,
            comment="Matches the run_id field in the blotter artifact JSON.",
        ),
        sa.Column(
            "blotter_local_path",
            sa.Text(),
            nullable=False,
            comment="Local filesystem path of the reviewed blotter artifact.",
        ),
        sa.Column(
            "blotter_sha256",
            sa.Text(),
            nullable=False,
            comment="SHA-256 hex digest of the blotter artifact file at review time.",
        ),
        sa.Column(
            "selected_order_ids",
            JSONB(),
            nullable=False,
            comment="JSON list of candidate sequence numbers approved for submission, or ['ALL'].",
        ),
        sa.Column(
            "approved_at_utc",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "approved_by",
            sa.Text(),
            nullable=False,
            comment="Operator identifier (email or username) who approved.",
        ),
        sa.Column(
            "confirmed_blotter_sha256",
            sa.Text(),
            nullable=False,
            comment=(
                "SHA-256 computed by the operator/dashboard at review time. "
                "The sensor verifies this matches blotter_sha256 in Python before "
                "permitting submission. Kept as independent fields to support the "
                "future Streamlit dashboard (M5.8) which may set them independently."
            ),
        ),
        sa.Column(
            "dashboard_session_id",
            sa.Text(),
            nullable=True,
            comment="Streamlit session ID when approved via dashboard (future use).",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("blotter_run_id", name="uq_blotter_approvals_run_id"),
        sa.CheckConstraint(
            "length(blotter_sha256) = 64",
            name="ck_blotter_approvals_sha256_len",
        ),
        sa.CheckConstraint(
            "length(confirmed_blotter_sha256) = 64",
            name="ck_blotter_approvals_confirmed_sha256_len",
        ),
    )

    op.create_index(
        "ix_blotter_approvals_run_id",
        "blotter_approvals",
        ["blotter_run_id"],
    )
    op.create_index(
        "ix_blotter_approvals_approved_at",
        "blotter_approvals",
        ["approved_at_utc"],
    )


def downgrade() -> None:
    op.drop_index("ix_blotter_approvals_approved_at", table_name="blotter_approvals")
    op.drop_index("ix_blotter_approvals_run_id", table_name="blotter_approvals")
    op.drop_table("blotter_approvals")
