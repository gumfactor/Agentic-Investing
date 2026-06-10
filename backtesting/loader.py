"""Backtest data loader: assembles a DataHandler from versioned MinIO snapshots.

Price adjustment pipeline
-------------------------
Prices stored in daily_prices are unadjusted (per the Phase 1 design decision).
This loader applies corporate-action adjustment factors before constructing
DataHandler so the backtest engine always operates on split- and
dividend-adjusted closes.  Using unadjusted prices in a backtest produces
fictitious P&L wherever a split occurs (Codex finding #3).

Snapshot convention
-------------------
All data types share a single snapshot date (the data_version), which is the
date the snapshots were pinned.  The MinIO paths are:

    rqis-snapshots/snapshots/daily_prices/{data_version}/data.parquet
    rqis-snapshots/snapshots/corporate_actions/{data_version}/data.parquet
    rqis-snapshots/snapshots/alpha_scores/{data_version}/data.parquet
    rqis-snapshots/snapshots/benchmark/{data_version}/data.parquet

The corporate_actions snapshot is optional: if absent, an empty frame is
used and all adj_factors default to 1.0 (no adjustment).
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd
import structlog

from data.normalization.corporate_actions import (
    apply_adjustment_factors,
    compute_adjustment_factors,
)
from backtesting.engine.data_handler import DataHandler

logger = structlog.get_logger(__name__)


def load_from_snapshot(
    data_version: str,
    config: dict,
    snapshots: Optional[ParquetSnapshots] = None,
) -> DataHandler:
    """Load a DataHandler from versioned MinIO snapshots with price adjustment.

    Args:
        data_version: Snapshot date string ('YYYY-MM-DD').  All required data
            types must have a snapshot pinned on this date.
        config: Strategy config dict.  Used to filter alpha_scores by
            strategy_id (``config["name"]`` or ``"v1"`` as fallback).
        snapshots: ParquetSnapshots instance.  If None, one is created from
            MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY env vars.

    Returns:
        DataHandler ready for use in BacktestEngine.run().

    Raises:
        FileNotFoundError: if daily_prices, alpha_scores, or benchmark snapshots
            are absent for data_version.
        ValueError: if a required column is missing from a loaded snapshot.
    """
    from data.storage.parquet_snapshots import ParquetSnapshots  # lazy: avoids minio at import time
    snaps = snapshots or ParquetSnapshots()
    snap_date = date.fromisoformat(data_version)
    strategy_id = config.get("name", "v1")

    logger.info(
        "loader_start",
        data_version=data_version,
        strategy_id=strategy_id,
    )

    # ── Load raw snapshots ────────────────────────────────────────────────────
    prices_raw = snaps.load_snapshot("daily_prices", snap_date)

    try:
        corp_actions = snaps.load_snapshot("corporate_actions", snap_date)
    except FileNotFoundError:
        logger.warning(
            "loader_no_corp_actions",
            data_version=data_version,
            note="all adj_factors default to 1.0",
        )
        corp_actions = pd.DataFrame(
            columns=["ticker", "ex_date", "action_type", "value"]
        )

    alpha_scores = snaps.load_snapshot("alpha_scores", snap_date)
    benchmark = snaps.load_snapshot("benchmark", snap_date)

    # ── Filter alpha_scores to this strategy ─────────────────────────────────
    if "strategy_id" in alpha_scores.columns:
        alpha_scores = alpha_scores[
            alpha_scores["strategy_id"] == strategy_id
        ].reset_index(drop=True)

    # ── Apply price adjustment ────────────────────────────────────────────────
    prices_adj = _adjust_prices(prices_raw, corp_actions)

    logger.info(
        "loader_complete",
        prices_rows=len(prices_adj),
        alpha_rows=len(alpha_scores),
        benchmark_rows=len(benchmark),
        tickers=prices_adj["ticker"].nunique() if not prices_adj.empty else 0,
    )

    return DataHandler(prices_adj, alpha_scores, benchmark)


def _adjust_prices(
    prices: pd.DataFrame,
    corp_actions: pd.DataFrame,
) -> pd.DataFrame:
    """Compute and apply adjustment factors; replace close with adj_close.

    Returns prices DataFrame with the ``close`` column replaced by the
    split- and dividend-adjusted close price (float dtype).
    """
    factors = compute_adjustment_factors(corp_actions, prices)
    adjusted = apply_adjustment_factors(prices, factors)

    result = adjusted.copy()
    # adj_close may contain Decimal objects; cast to float for DataHandler.
    result["close"] = result["adj_close"].apply(
        lambda v: float(v) if v is not None else float("nan")
    )
    return result
