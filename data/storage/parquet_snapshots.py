"""Parquet snapshot management via MinIO.

Snapshots serve two purposes:
  1. Reproducible backtests: pin a backtest to a specific snapshot version
     so re-running it years later produces identical results even if the DB
     has been corrected. (See C7 in PRD.md.)
  2. Fast batch reads: reading a full 5-year history from parquet is much
     faster than a DB query for cross-sectional signal computation.

Snapshot naming convention:
    {bucket}/snapshots/{data_type}/{YYYY-MM-DD}/data.parquet
    e.g. rqis-snapshots/snapshots/daily_prices/2026-06-05/data.parquet

A "snapshot" is a full dump of a table as of a given date. The snapshot
date is the date the snapshot was taken, not the data's own date range.
"""

from __future__ import annotations

import io
import os
from datetime import date
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import structlog
from minio import Minio
from minio.error import S3Error

logger = structlog.get_logger(__name__)

_DEFAULT_BUCKET = os.environ.get("MINIO_BUCKET_SNAPSHOTS", "rqis-snapshots")


class ParquetSnapshots:
    """Read/write parquet snapshots to MinIO object storage.

    Args:
        endpoint: MinIO endpoint (host:port). Defaults to MINIO_ENDPOINT env var.
        access_key: MinIO access key. Defaults to MINIO_ACCESS_KEY env var.
        secret_key: MinIO secret key. Defaults to MINIO_SECRET_KEY env var.
        bucket: Target bucket name.
        secure: Use TLS. Default False for local dev.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        bucket: str = _DEFAULT_BUCKET,
        secure: bool = False,
    ) -> None:
        self._client = Minio(
            endpoint=endpoint or os.environ["MINIO_ENDPOINT"],
            access_key=access_key or os.environ["MINIO_ACCESS_KEY"],
            secret_key=secret_key or os.environ["MINIO_SECRET_KEY"],
            secure=secure,
        )
        self._bucket = bucket
        self._ensure_bucket()

    def save_snapshot(
        self,
        df: pd.DataFrame,
        data_type: str,
        snapshot_date: Optional[date] = None,
    ) -> str:
        """Save a DataFrame as a parquet snapshot.

        Args:
            df          : DataFrame to snapshot.
            data_type   : Logical name (e.g., 'daily_prices', 'alpha_scores').
            snapshot_date: Date label for the snapshot. Defaults to today.

        Returns:
            The MinIO object path (bucket/key) — store this as the data_version
            in MLflow to satisfy C7 (pinned data snapshot).
        """
        snap_date = snapshot_date or date.today()
        key = f"snapshots/{data_type}/{snap_date}/data.parquet"

        table = pa.Table.from_pandas(df, preserve_index=False)
        buffer = io.BytesIO()
        pq.write_table(table, buffer, compression="snappy")
        buffer.seek(0)

        self._client.put_object(
            bucket_name=self._bucket,
            object_name=key,
            data=buffer,
            length=buffer.getbuffer().nbytes,
            content_type="application/octet-stream",
        )

        path = f"{self._bucket}/{key}"
        logger.info(
            "snapshot_saved",
            path=path,
            rows=len(df),
            data_type=data_type,
            snapshot_date=str(snap_date),
        )
        return path

    def load_snapshot(self, data_type: str, snapshot_date: date) -> pd.DataFrame:
        """Load a parquet snapshot for a given data_type and date.

        Raises:
            FileNotFoundError: if no snapshot exists for that date.
        """
        key = f"snapshots/{data_type}/{snapshot_date}/data.parquet"
        try:
            response = self._client.get_object(self._bucket, key)
            buffer = io.BytesIO(response.read())
        except S3Error as exc:
            raise FileNotFoundError(
                f"No snapshot found at {self._bucket}/{key}"
            ) from exc

        df = pd.read_parquet(buffer)
        logger.info(
            "snapshot_loaded",
            path=f"{self._bucket}/{key}",
            rows=len(df),
            data_type=data_type,
            snapshot_date=str(snapshot_date),
        )
        return df

    def list_snapshots(self, data_type: str) -> list[date]:
        """List available snapshot dates for a given data_type, sorted newest-first."""
        prefix = f"snapshots/{data_type}/"
        objects = self._client.list_objects(self._bucket, prefix=prefix, recursive=False)
        dates = []
        for obj in objects:
            # Object name pattern: snapshots/{data_type}/{YYYY-MM-DD}/
            parts = obj.object_name.rstrip("/").split("/")
            if len(parts) >= 3:
                try:
                    dates.append(date.fromisoformat(parts[2]))
                except ValueError:
                    pass
        return sorted(dates, reverse=True)

    def save_raw_response(self, data: bytes, source: str, data_type: str, batch_id: str) -> str:
        """Store a raw API response before transformation.

        This supports idempotent reprocessing: if transformation fails,
        the raw data is preserved and can be re-transformed without
        hitting the API again. Path is returned for logging in data_ingestion_log.
        """
        key = f"raw/{source}/{data_type}/{batch_id}/response.json"
        buffer = io.BytesIO(data)
        self._client.put_object(
            bucket_name=os.environ.get("MINIO_BUCKET_RAW", "rqis-raw"),
            object_name=key,
            data=buffer,
            length=len(data),
            content_type="application/json",
        )
        return f"rqis-raw/{key}"

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _ensure_bucket(self) -> None:
        raw_bucket = os.environ.get("MINIO_BUCKET_RAW", "rqis-raw")
        for bucket in [self._bucket, raw_bucket]:
            if not self._client.bucket_exists(bucket):
                self._client.make_bucket(bucket)
                logger.info("bucket_created", bucket=bucket)
