"""Fundamentals schema: financial_statements, sec_filings.

Revision ID: 003
Revises: 002
Create Date: 2026-06-09

Point-in-time correctness:
  period_end_date  — last calendar day of the fiscal period reported
  release_date     — date the data became publicly available (filing date)

Only rows where release_date <= as_of_date are visible to the signal layer.
The pit_join() utility in data/normalization/point_in_time.py enforces this.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: str = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── sec_filings ──────────────────────────────────────────────────────────
    # Metadata index for EDGAR filings (10-K, 10-Q, 8-K).
    # Stores the filing date (= release_date for financial data in that filing)
    # and a pointer to the raw filing in object storage.
    op.create_table(
        "sec_filings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("cik", sa.String(20), nullable=False),
        sa.Column("accession_number", sa.String(25), nullable=False),
        sa.Column("form_type", sa.String(20), nullable=False),
        sa.Column("period_end_date", sa.Date(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("raw_storage_path", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("accession_number", name="uq_sec_filings_accession"),
        sa.CheckConstraint(
            "form_type IN ('10-K', '10-Q', '8-K', '10-K/A', '10-Q/A')",
            name="ck_sec_filings_form_type",
        ),
    )
    op.create_index(
        "ix_sec_filings_ticker_period",
        "sec_filings",
        ["ticker", sa.text("period_end_date DESC")],
    )
    op.create_index(
        "ix_sec_filings_filing_date",
        "sec_filings",
        [sa.text("filing_date DESC")],
    )

    # ── financial_statements ─────────────────────────────────────────────────
    # Normalised fundamental line items from income statement, balance sheet,
    # and cash flow statement. One row per (ticker, period_end_date, item_name).
    #
    # Storing as EAV (entity-attribute-value) rather than wide columns lets us
    # add new line items without schema migrations. The signal layer aggregates
    # into wide form at query time via pivot.
    #
    # item_name examples: 'revenue', 'net_income', 'total_assets',
    #   'total_debt', 'equity', 'free_cash_flow', 'operating_income'
    # period_type: 'annual' | 'quarterly' | 'ttm'
    # source: 'simfin' | 'sec_edgar' | 'yfinance'
    op.create_table(
        "financial_statements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("period_end_date", sa.Date(), nullable=False),
        sa.Column("release_date", sa.Date(), nullable=False),
        sa.Column("period_type", sa.String(20), nullable=False),
        sa.Column("item_name", sa.String(100), nullable=False),
        sa.Column("value", sa.Numeric(24, 6), nullable=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_version", sa.String(50), nullable=True),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ticker", "period_end_date", "period_type", "item_name", "source",
            name="uq_financial_statements_key",
        ),
        sa.CheckConstraint(
            "period_type IN ('annual', 'quarterly', 'ttm')",
            name="ck_financial_statements_period_type",
        ),
    )
    op.create_index(
        "ix_financial_statements_ticker_period",
        "financial_statements",
        ["ticker", sa.text("period_end_date DESC"), "item_name"],
    )
    op.create_index(
        "ix_financial_statements_release_date",
        "financial_statements",
        [sa.text("release_date DESC")],
    )
    # Partial index: quickly find the most recently released value per item
    # as of any given as_of_date (supports pit_join).
    op.create_index(
        "ix_financial_statements_pit",
        "financial_statements",
        ["ticker", "item_name", sa.text("release_date DESC"), "period_type"],
    )


def downgrade() -> None:
    op.drop_table("financial_statements")
    op.drop_table("sec_filings")
