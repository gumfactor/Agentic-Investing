"""Versioned dataset manifest for reproducible backtests.

A DatasetManifest is a JSON record that fully describes every data source
used in a backtest run: which MinIO snapshots were loaded, their row counts,
date coverage, schema fingerprints, and the git commit that produced the
signals.  Storing the manifest path as the MLflow data_version satisfies C7
and gives a richer provenance record than a prices-only snapshot path.

Problem addressed (Codex finding #1)
-------------------------------------
The previous practice was to use the MinIO path of the *prices* snapshot as
data_version.  This left alpha scores, corporate actions, and the benchmark
unversioned — a run could be "reproduced" with stale or corrected signals
while appearing to use the same data_version.  The manifest bundles all four
sources into a single versioned object.

Manifest path convention
------------------------
    {bucket}/manifests/{version}/manifest.json
    e.g. rqis-snapshots/manifests/2026-06-10/manifest.json

Usage
-----
    # When pinning a new dataset bundle:
    manifest = build_manifest(
        version="2026-06-10",
        strategy_id="v1",
        dataframes={"daily_prices": prices_df, "alpha_scores": alpha_df, ...},
        object_paths={"daily_prices": "rqis-snapshots/snapshots/...", ...},
        snapshot_dates={"daily_prices": date(2026, 6, 10), ...},
    )
    manifest_path = save_manifest(manifest, minio_client)

    # When loading for a backtest:
    manifest = load_manifest("2026-06-10", minio_client)
    data_version = manifest_path  # pass to BacktestLogger.log_run()
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# Data types that participate in a standard backtest bundle.
_BUNDLE_TYPES = ("daily_prices", "alpha_scores", "corporate_actions", "benchmark")

# Column that holds the primary date for each data type.
_DATE_COL = {
    "daily_prices": "date",
    "alpha_scores": "score_date",
    "corporate_actions": "ex_date",
    "benchmark": "date",
}


@dataclass
class DatasetManifest:
    """Complete record of all data sources used in one backtest run."""
    version: str                         # snapshot date, e.g. "2026-06-10"
    created_at: str                      # ISO 8601 UTC timestamp
    git_commit: str                      # HEAD sha or "unknown"
    strategy_id: str
    snapshot_dates: dict[str, str]       # data_type → YYYY-MM-DD
    object_paths: dict[str, str]         # data_type → MinIO bucket/key
    row_counts: dict[str, int]           # data_type → number of rows
    date_ranges: dict[str, list[str]]    # data_type → [min_date, max_date]
    schema_hashes: dict[str, str]        # data_type → sha256[:16] of sorted columns


def build_manifest(
    version: str,
    strategy_id: str,
    dataframes: dict[str, pd.DataFrame],
    object_paths: dict[str, str],
    snapshot_dates: dict[str, date],
) -> DatasetManifest:
    """Build a DatasetManifest from already-loaded DataFrames.

    Args:
        version: Snapshot version string (YYYY-MM-DD).
        strategy_id: Strategy identifier used to filter alpha_scores.
        dataframes: Mapping of data_type → loaded DataFrame.
        object_paths: Mapping of data_type → MinIO object path.
        snapshot_dates: Mapping of data_type → snapshot date (``date`` object).

    Returns:
        DatasetManifest ready to save via save_manifest().
    """
    row_counts: dict[str, int] = {}
    date_ranges: dict[str, list[str]] = {}
    schema_hashes: dict[str, str] = {}

    for data_type, df in dataframes.items():
        row_counts[data_type] = len(df)
        date_col = _DATE_COL.get(data_type)
        if date_col and date_col in df.columns and not df.empty:
            col = pd.to_datetime(df[date_col], errors="coerce")
            date_ranges[data_type] = [
                str(col.min().date()),
                str(col.max().date()),
            ]
        schema_hashes[data_type] = hashlib.sha256(
            "|".join(sorted(df.columns)).encode()
        ).hexdigest()[:16]

    return DatasetManifest(
        version=version,
        created_at=datetime.now(timezone.utc).isoformat(),
        git_commit=_git_commit(),
        strategy_id=strategy_id,
        snapshot_dates={k: str(v) for k, v in snapshot_dates.items()},
        object_paths=object_paths,
        row_counts=row_counts,
        date_ranges=date_ranges,
        schema_hashes=schema_hashes,
    )


def save_manifest(manifest: DatasetManifest, minio_client, bucket: str) -> str:
    """Serialise and write manifest to MinIO.

    Args:
        manifest: Built by build_manifest().
        minio_client: Minio client instance.
        bucket: Target bucket (e.g. "rqis-snapshots").

    Returns:
        Full MinIO path "bucket/manifests/{version}/manifest.json".
    """
    key = f"manifests/{manifest.version}/manifest.json"
    payload = json.dumps(asdict(manifest), indent=2).encode()
    buf = io.BytesIO(payload)
    minio_client.put_object(
        bucket_name=bucket,
        object_name=key,
        data=buf,
        length=len(payload),
        content_type="application/json",
    )
    path = f"{bucket}/{key}"
    logger.info("manifest_saved", path=path, version=manifest.version)
    return path


def load_manifest(version: str, minio_client, bucket: str) -> DatasetManifest:
    """Load a manifest from MinIO by version string.

    Raises:
        FileNotFoundError: if no manifest exists for version.
    """
    from minio.error import S3Error  # lazy import to keep module importable without minio

    key = f"manifests/{version}/manifest.json"
    try:
        response = minio_client.get_object(bucket, key)
        data = json.loads(response.read())
    except S3Error as exc:
        raise FileNotFoundError(f"No manifest at {bucket}/{key}") from exc
    # Filter to known fields so manifests written by a newer code version
    # (with extra fields) can still be loaded by older code without TypeError.
    known = DatasetManifest.__dataclass_fields__
    return DatasetManifest(**{k: v for k, v in data.items() if k in known})


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _git_commit() -> str:
    """Return the current HEAD commit hash, or 'unknown' on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"
