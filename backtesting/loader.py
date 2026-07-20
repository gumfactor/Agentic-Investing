"""Backtest data loader: assembles a DataHandler from versioned MinIO snapshots.

Price adjustment pipeline
-------------------------
Prices stored in daily_prices are unadjusted (per the Phase 1 design decision).
This loader applies corporate-action adjustment factors before constructing
DataHandler so the backtest engine always operates on split- and
dividend-adjusted closes.  Using unadjusted prices in a backtest produces
fictitious P&L wherever a split occurs (Codex finding #3).

Snapshot convention (03A-1, content-addressed)
----------------------------------------------
`data_version` is a `DatasetManifest.manifest_content_sha256` -- the content
hash of the pinned bundle's manifest (`manifests/{data_version}/manifest.json`).
The manifest records, per data type, the canonical LOGICAL content hash of
that dataframe; the loader resolves the manifest and loads each data type by
its manifest-recorded content hash via
`ParquetSnapshots.load_snapshot_by_manifest`, which re-verifies the
downloaded content's hash on the way in (tamper-evidence, section 2.3).

    rqis-snapshots/manifests/{data_version}/manifest.json
    rqis-snapshots/snapshots/{data_type}/sha256/{h2}/{content_hash}/data.parquet

The corporate_actions snapshot is optional: if the manifest has no
corporate_actions entry, or the referenced object is absent, an empty frame
is used and all adj_factors default to 1.0 (no adjustment). (Narrowing this
optional path to only genuine not-found errors -- so an infra/auth failure
can no longer masquerade as "no corporate actions" -- is BUG-039's fix,
scoped to 03A-2; 03A-1 preserves the existing optional-frame behavior.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import structlog

from backtesting.config_contract import validate_backtest_config
from backtesting.engine.data_handler import DataHandler
from data.normalization.corporate_actions import (
    apply_adjustment_factors,
    compute_adjustment_factors,
)

if TYPE_CHECKING:
    from data.storage.parquet_snapshots import ParquetSnapshots

logger = structlog.get_logger(__name__)


def load_from_snapshot(
    data_version: str,
    config: dict,
    snapshots: ParquetSnapshots | None = None,
) -> DataHandler:
    """Load a DataHandler from versioned MinIO snapshots with price adjustment.

    Args:
        data_version: A ``DatasetManifest.manifest_content_sha256`` -- the
            content hash identifying the pinned bundle's manifest. All
            required data types are loaded by the content hash the manifest
            records for them.
        config: Strategy config dict. Used to filter alpha_scores by
            ``strategy_id``, then ``name``, or ``"v1"`` as fallback.
        snapshots: ParquetSnapshots instance.  If None, one is created from
            MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY env vars.

    Returns:
        DataHandler ready for use in BacktestEngine.run().

    Raises:
        FileNotFoundError: if daily_prices, alpha_scores, or benchmark snapshots
            are absent for data_version.
        ValueError: if a required column is missing from a loaded snapshot,
            or if zero alpha_scores rows match the resolved strategy_id
            (fail-closed -- a silent cash-only backtest mislabeled with the
            strategy's name is never acceptable; 02B round-2 P0-2).
        UnsupportedStrategyConfigError: ``config`` declares a field, section,
            or value the backtest path does not implement (Roadmap 02B /
            BUG-075, fail-closed -- see ``backtesting/config_contract.py``).
            Checked here, first in the backtest pipeline, so a rejected
            config never even reaches the expensive snapshot-loading step.
    """
    validate_backtest_config(config)

    if snapshots is None:
        from dotenv import load_dotenv

        load_dotenv()
        from data.storage.parquet_snapshots import (
            ParquetSnapshots,  # lazy: avoids minio at import time
        )

        snapshots = ParquetSnapshots()
    snaps = snapshots
    strategy_id = config.get("strategy_id", config.get("name", "v1"))

    logger.info(
        "loader_start",
        data_version=data_version,
        strategy_id=strategy_id,
    )

    # ── Resolve the bundle manifest, then load each data type by the content
    #    hash the manifest recorded for it (03A-1 content addressing). ─────────
    from backtesting.dataset_manifest import load_manifest  # lazy: avoids minio

    manifest = load_manifest(data_version, snaps._client, snaps._bucket)

    # ── Load raw snapshots ────────────────────────────────────────────────────
    prices_raw = snaps.load_snapshot_by_manifest(manifest, "daily_prices")

    try:
        corp_actions = snaps.load_snapshot_by_manifest(manifest, "corporate_actions")
    except FileNotFoundError:
        logger.warning(
            "loader_no_corp_actions",
            data_version=data_version,
            note="all adj_factors default to 1.0",
        )
        corp_actions = pd.DataFrame(
            columns=["ticker", "ex_date", "action_type", "value"]
        )

    alpha_scores = snaps.load_snapshot_by_manifest(manifest, "alpha_scores")
    benchmark = snaps.load_snapshot_by_manifest(manifest, "benchmark")

    # ── Filter alpha_scores to this strategy ─────────────────────────────────
    # The strategy_id column is required by the alpha_scores schema (migration 002).
    # If it is absent the snapshot was built incorrectly; passing it through
    # would silently mix signals from every strategy into DataHandler.
    if "strategy_id" not in alpha_scores.columns:
        raise ValueError(
            f"alpha_scores snapshot for data_version={data_version!r} is missing "
            f"the 'strategy_id' column. Re-pin the snapshot using a pipeline version "
            f"that writes strategy_id, or verify the correct snapshot was loaded."
        )
    alpha_scores = alpha_scores[
        alpha_scores["strategy_id"] == strategy_id
    ].reset_index(drop=True)

    if alpha_scores.empty:
        # Fail closed (02B round-2 P0-2): an empty post-filter frame means
        # the resolved strategy_id matched ZERO stored score rows -- almost
        # always a strategy_id/name mismatch (stored score IDs can differ
        # from YAML display names, which is exactly why the paper-path
        # scripts require an explicit --strategy-id). Continuing here used
        # to produce a silent cash-only backtest labeled with the
        # strategy's declared name -- a mislabeled result, not a degraded
        # one.
        raise ValueError(
            f"alpha_scores snapshot for data_version={data_version!r} contains "
            f"zero rows for strategy_id={strategy_id!r} (resolved from "
            "config['strategy_id'], falling back to config['name'], then 'v1'). "
            "Running would produce a cash-only backtest silently labeled as "
            f"{strategy_id!r}. Either the strategy_id/name in the config does "
            "not match the id the scores were stored under, or the score "
            "backfill has not been run for this strategy on this snapshot. "
            "Set an explicit top-level strategy_id in the config matching the "
            "stored score rows, or re-pin/backfill the snapshot."
        )

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
