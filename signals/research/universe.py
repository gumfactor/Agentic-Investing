"""Universe survivorship bias audit.

The Phase 2 universe is current-membership S&P 500 (503 tickers, 5-year
price history).  Because we use *today's* members, companies that were
removed from the index between 2021 and 2026 are absent.  Removed
companies tend to be underperformers or firms that were acquired or went
bankrupt — exactly the companies that would have *weak* factor scores.
Excluding them biases cross-sectional IC upward.

This module provides tools to:
  1. Quantify the bias from the available price data.
  2. Attach a structured warning to IC summaries and reports.
  3. Guide interpretation: how much of a positive IC result might be
     artefact vs signal.

Usage
-----
    from signals.research.universe import audit_universe_survivorship, label_ic_with_bias

    audit = audit_universe_survivorship(prices)
    ic_summary = label_ic_with_bias(ic_summary, audit)

Remedy (Phase 3)
----------------
Replace the static universe with a point-in-time constituent CSV sourced
from a vendor (e.g. CRSP, Compustat, or a scraped historical S&P 500
membership table).  Until then, treat all IC results as *provisional*.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# Thresholds for bias severity classification
_LATE_ENTRANT_LOW_THRESHOLD = 0.10   # < 10 % late entrants → low
_LATE_ENTRANT_HIGH_THRESHOLD = 0.25  # > 25 % late entrants → high
# A ticker is a "late entrant" if its first price date is more than this
# many days after the overall universe start date.  63 days = ~1 quarter.
_LATE_ENTRANT_LAG_DAYS = 63


def audit_universe_survivorship(prices: pd.DataFrame) -> dict:
    """Estimate survivorship bias in a price universe.

    Examines the distribution of first price dates per ticker.  Tickers
    whose history begins significantly after the overall start date are
    proxy evidence that older, potentially weaker companies were excluded.

    This is a heuristic — we cannot know the *true* bias without historical
    S&P 500 constituent data — but it quantifies the lower bound and
    provides a consistent label for IC output.

    Args:
        prices: Long-format DataFrame with columns ``ticker`` and ``date``.
    Returns:
        dict with keys:
          - ``total_tickers`` (int)
          - ``start_date``, ``end_date`` (date)
          - ``calendar_days`` (int)
          - ``non_late_entrant_count`` (int): tickers whose first date is
            within ``_LATE_ENTRANT_LAG_DAYS`` days of the universe start.
            NOTE: this includes tickers that arrived up to 63 days after
            start, NOT necessarily from day 1.
          - ``late_entrant_count`` (int): tickers whose first date is >
            start + ``_LATE_ENTRANT_LAG_DAYS``
          - ``late_entrant_fraction`` (float)
          - ``bias_severity`` (str): "low" / "moderate" / "high"
          - ``median_history_days`` (int): median per-ticker history length
          - ``min_history_days`` (int): shortest per-ticker history.
            A value of 0 means at least one ticker has only a single row.
          - ``warning`` (str): human-readable label for reports
    """
    required = {"ticker", "date"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices missing columns: {missing}")
    if prices.empty:
        raise ValueError("prices DataFrame is empty")

    prices = prices.copy()
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    universe_start = prices["date"].min()
    universe_end = prices["date"].max()
    calendar_days = (universe_end - universe_start).days

    late_cutoff = universe_start + timedelta(days=_LATE_ENTRANT_LAG_DAYS)

    per_ticker = (
        prices.groupby("ticker")["date"]
        .agg(first_date="min", last_date="max", n_days="count")
        .reset_index()
    )
    # Convert to pd.Timestamp for safe vectorised day arithmetic
    per_ticker["first_date"] = pd.to_datetime(per_ticker["first_date"])
    per_ticker["last_date"] = pd.to_datetime(per_ticker["last_date"])
    per_ticker["history_days"] = (
        (per_ticker["last_date"] - per_ticker["first_date"]).dt.days
    )
    # Convert back to date objects for consistent comparison
    per_ticker["first_date"] = per_ticker["first_date"].dt.date

    total = len(per_ticker)
    non_late = int((per_ticker["first_date"] <= late_cutoff).sum())
    late_entrants = total - non_late
    late_fraction = late_entrants / total if total > 0 else 0.0

    median_hist = int(per_ticker["history_days"].median())
    min_hist = int(per_ticker["history_days"].min())

    if late_fraction < _LATE_ENTRANT_LOW_THRESHOLD:
        severity = "low"
    elif late_fraction > _LATE_ENTRANT_HIGH_THRESHOLD:
        severity = "high"
    else:
        severity = "moderate"

    warning = (
        f"SURVIVORSHIP BIAS [{severity.upper()}]: universe is current-membership "
        f"S&P 500 ({total} tickers). {late_entrants} tickers ({late_fraction:.0%}) "
        f"entered after {universe_start}. Companies removed from the index "
        f"(underperformers, bankruptcies, mergers) are absent. IC results "
        f"are provisional until point-in-time constituent history is used."
    )

    result = {
        "total_tickers": total,
        "start_date": universe_start,
        "end_date": universe_end,
        "calendar_days": calendar_days,
        "non_late_entrant_count": non_late,
        "late_entrant_count": late_entrants,
        "late_entrant_fraction": round(late_fraction, 4),
        "bias_severity": severity,
        "median_history_days": median_hist,
        "min_history_days": min_hist,
        "warning": warning,
    }

    logger.info(
        "universe_survivorship_audit",
        total_tickers=total,
        late_entrant_fraction=f"{late_fraction:.1%}",
        bias_severity=severity,
        calendar_days=calendar_days,
    )
    return result


def universe_size_by_date(prices: pd.DataFrame) -> pd.DataFrame:
    """Count available tickers per date.

    Returns:
        DataFrame with columns [date, ticker_count].
        Useful for detecting universe gaps and cross-checking against
        expected S&P 500 membership counts.
    """
    required = {"ticker", "date"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices missing columns: {missing}")

    result = (
        prices.groupby("date")["ticker"]
        .nunique()
        .reset_index()
        .rename(columns={"ticker": "ticker_count"})
        .sort_values("date")
        .reset_index(drop=True)
    )
    return result


def label_ic_with_bias(
    ic_df: pd.DataFrame,
    audit: dict,
) -> pd.DataFrame:
    """Attach survivorship bias metadata to an IC summary DataFrame.

    Adds a ``survivorship_bias_severity`` column and a
    ``survivorship_bias_warning`` column so any downstream consumer
    (report, notebook, DB write) carries the provenance.

    Args:
        ic_df: Output of :func:`~signals.research.ic.summarize_ic` or
            similar IC summary DataFrame.
        audit: Output of :func:`audit_universe_survivorship`.

    Returns:
        Copy of ``ic_df`` with two additional columns appended.
    """
    out = ic_df.copy()
    out["survivorship_bias_severity"] = audit["bias_severity"]
    out["survivorship_bias_warning"] = audit["warning"]
    return out
