"""Shared point-in-time eligibility helpers for composite signals (BUG-008).

Cross-sectional statistics (z-scores, ranks) must be computed over the
point-in-time eligible universe, not over every priced ticker — filtering
output rows after standardization leaves non-members contaminating the
cross-sectional mean/std (Codex PR #34 review, rounds 4-5). Each composite
accepts an optional ``eligibility`` long-format DataFrame (``ticker``,
``date`` columns listing ELIGIBLE pairs) and applies it BEFORE its
cross-sectional step via these helpers.

Conventions:
- Raw per-ticker statistics (window returns, rolling vols, betas, ratios)
  still use each ticker's full history — lookbacks spanning a ticker's
  pre-membership period are unaffected. Only the cross-section is masked.
- Dates present in the data but absent from ``eligibility`` are fully
  masked (fail closed).
- ``eligibility=None`` keeps the legacy behavior (every priced ticker in
  the cross-section); such outputs are PROVISIONAL per the 01B design plan.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


def validate_eligibility_frame(eligibility: pd.DataFrame) -> None:
    required = {"ticker", "date"}
    missing = required - set(eligibility.columns)
    if missing:
        raise ValueError(f"eligibility DataFrame missing columns: {missing}")


def build_wide_eligibility_mask(
    eligibility: pd.DataFrame,
    index: pd.Index,
    columns: pd.Index,
) -> pd.DataFrame:
    """Boolean date×ticker mask aligned to a wide frame's index/columns.

    Cells hold True where the (date, ticker) pair is eligible; pairs never
    listed — including entire dates absent from ``eligibility`` — are False
    (fail closed).
    """
    validate_eligibility_frame(eligibility)
    flags = eligibility[["ticker", "date"]].copy()
    flags["eligible"] = True
    mask = (
        flags.pivot_table(index="date", columns="ticker", values="eligible", aggfunc="any")
        .reindex(index=index, columns=columns)
        .notna()
    )
    mask.columns.name = None
    return mask


def eligibility_sets_by_date(eligibility: pd.DataFrame) -> dict:
    """``{date: set(tickers)}`` view of an eligibility frame."""
    validate_eligibility_frame(eligibility)
    return {
        d: set(group["ticker"])
        for d, group in eligibility.groupby("date")
    }
