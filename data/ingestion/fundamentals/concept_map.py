"""XBRL concept → normalized item_name mapping for SEC EDGAR ingestion.

Each entry maps our internal item_name to a prioritized list of XBRL concept
paths (namespace/ConceptName).  The first concept found in a company's filing
is used.  Multiple aliases exist because companies choose their own tags.

Derived items (free_cash_flow, earnings_yield) are computed from base items
after ingestion — they are not fetched directly from EDGAR.

Unit conventions (stored in financial_statements.value):
  Monetary items : USD (raw, not thousands/millions — EDGAR stores as reported)
  Share items    : count
"""

from __future__ import annotations

# Base XBRL concepts to fetch from companyfacts JSON.
# key   = our normalized item_name (stored in financial_statements.item_name)
# value = ordered list of XBRL concepts; first match wins
CONCEPT_ALIASES: dict[str, list[str]] = {
    # ── Income statement ───────────────────────────────────────────────────
    "revenue": [
        "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap/Revenues",
        "us-gaap/SalesRevenueNet",
        "us-gaap/RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "gross_profit": [
        "us-gaap/GrossProfit",
    ],
    "operating_income": [
        "us-gaap/OperatingIncomeLoss",
    ],
    "net_income": [
        "us-gaap/NetIncomeLoss",
        "us-gaap/ProfitLoss",
        "us-gaap/NetIncomeLossAvailableToCommonStockholdersBasic",
    ],

    # ── Balance sheet ──────────────────────────────────────────────────────
    "total_assets": [
        "us-gaap/Assets",
    ],
    "total_equity": [
        "us-gaap/StockholdersEquity",
        "us-gaap/StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "total_debt": [
        "us-gaap/LongTermDebt",
        "us-gaap/DebtLongtermAndShorttermCombinedAmount",
    ],
    "shares_outstanding": [
        "us-gaap/CommonStockSharesOutstanding",
        "us-gaap/SharesOutstanding",
    ],

    # ── Cash flow ──────────────────────────────────────────────────────────
    "operating_cash_flow": [
        "us-gaap/NetCashProvidedByUsedInOperatingActivities",
    ],
    "capex": [
        "us-gaap/PaymentsToAcquirePropertyPlantAndEquipment",
        "us-gaap/CapitalExpendituresIncurredButNotYetPaid",
    ],
}

# Items derived by arithmetic from base items (computed post-ingestion).
# Each value is a tuple of (formula_description, required_base_items).
DERIVED_ITEMS: dict[str, tuple[str, list[str]]] = {
    "free_cash_flow": (
        "operating_cash_flow - capex",
        ["operating_cash_flow", "capex"],
    ),
}

# Acceptable EDGAR form types for fundamentals ingestion.
ACCEPTED_FORMS: frozenset[str] = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})

# Period duration bounds (calendar days) for classifying observations.
ANNUAL_MIN_DAYS = 340
ANNUAL_MAX_DAYS = 380
QUARTERLY_MIN_DAYS = 75
QUARTERLY_MAX_DAYS = 110
