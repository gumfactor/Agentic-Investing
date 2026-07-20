"""Parquet snapshot management via MinIO.

Snapshots serve two purposes:
  1. Reproducible backtests: pin a backtest to a specific snapshot version
     so re-running it years later produces identical results even if the DB
     has been corrected. (See C7 in PRD.md.)
  2. Fast batch reads: reading a full 5-year history from parquet is much
     faster than a DB query for cross-sectional signal computation.

Object layout (03A-1, content-addressed -- BUG-038):
    {bucket}/snapshots/{data_type}/sha256/{hash[0:2]}/{hash}/data.parquet
    e.g. rqis-snapshots/snapshots/daily_prices/sha256/ab/ab12.../data.parquet

`{hash}` is the canonical LOGICAL content hash of the DataFrame (see
`data.storage.canonical_hash.canonical_content_sha256`), not a hash of the
serialized parquet bytes -- parquet byte output is not deterministic across
writer versions/footers/compression, so a byte hash would break idempotent
re-pinning of identical data. Two different logical contents never map to the
same key, so nothing already written is ever overwritten (structurally closes
BUG-038, which used a caller-supplied `{snapshot_date}` as the key and
silently overwrote it on re-run). A human-readable `snapshot_date` is
preserved as manifest metadata only (see `backtesting.dataset_manifest`), not
as part of the object key.
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

from data.storage.canonical_hash import canonical_content_sha256

logger = structlog.get_logger(__name__)

_DEFAULT_BUCKET = os.environ.get("MINIO_BUCKET_SNAPSHOTS", "rqis-snapshots")


class SnapshotIntegrityError(Exception):
    """Raised when stored/downloaded snapshot content does not match its
    expected canonical content hash.

    Covers two situations, both fail-closed:
      - load time (section 2.3): a downloaded object's parsed DataFrame does
        not hash to the value recorded in the manifest (or encoded in the
        object's own content-addressed key) -- corruption or tampering.
      - save time (section 2.1): an object already exists at the computed
        content-addressed key, but re-downloading and re-hashing it does not
        match the hash that produced the key -- should be structurally
        impossible (the key derives from the hash), so this is defense
        against a hash-collision-shaped bug or manual tampering, not an
        expected code path.

    The full fail-closed object-store error taxonomy (SnapshotStoreUnavailable
    Error, SnapshotAccessDeniedError, SnapshotPartialReadError, and narrowing
    `FileNotFoundError` to a proper `SnapshotNotFoundError`) is 03A-2's scope;
    this exception only covers the content-integrity check needed here.
    """


def _content_key(data_type: str, content_sha256: str) -> str:
    return f"snapshots/{data_type}/sha256/{content_sha256[:2]}/{content_sha256}/data.parquet"


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
        """Save a DataFrame as a content-addressed parquet snapshot (03A-1).

        Computes the canonical LOGICAL content hash of `df` (section 2.1)
        *before* upload and keys the object by that hash. If an object
        already exists at that key, the content is already immutably stored
        and the write is skipped as a safe no-op -- verified by
        downloading/parsing the existing object and recomputing its
        canonical hash (not merely "key exists"), so a partial prior upload
        is never trusted as complete. Nothing is ever overwritten: two
        different logical contents never map to the same key.

        Args:
            df          : DataFrame to snapshot.
            data_type   : Logical name (e.g., 'daily_prices', 'alpha_scores').
            snapshot_date: Optional human-readable label recorded only in
                            logs/caller-side manifest metadata -- no longer
                            part of the object key (BUG-038).

        Returns:
            The MinIO object path (bucket/key) — store this (or the
            enclosing manifest's manifest_content_sha256) as the data_version
            in MLflow to satisfy C7 (pinned data snapshot).
        """
        content_hash = canonical_content_sha256(df, data_type)
        key = _content_key(data_type, content_hash)
        path = f"{self._bucket}/{key}"

        if self._object_exists(key):
            if self._verify_existing_object(key, data_type, content_hash):
                logger.info(
                    "snapshot_write_skipped_content_exists",
                    path=path,
                    rows=len(df),
                    data_type=data_type,
                    content_sha256=content_hash,
                )
                return path
            raise SnapshotIntegrityError(
                f"Existing object at content-addressed key {path} does not "
                f"hash to {content_hash}; refusing to treat it as this "
                "content and refusing to overwrite it."
            )

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

        logger.info(
            "snapshot_saved",
            path=path,
            rows=len(df),
            data_type=data_type,
            content_sha256=content_hash,
            snapshot_date=str(snapshot_date) if snapshot_date else None,
        )
        return path

    def load_snapshot(self, data_type: str, content_sha256: str) -> pd.DataFrame:
        """Load a content-addressed parquet snapshot by data_type and hash.

        Re-parses the downloaded parquet bytes and recomputes the canonical
        logical content hash (section 2.3), comparing it against
        `content_sha256`. This catches corruption from any out-of-band
        source (manual MinIO edit, bit rot, wrong bucket policy) even though
        the requested key already encodes the expected hash.

        Raises:
            FileNotFoundError: if no snapshot exists at that content key.
            SnapshotIntegrityError: if the downloaded content's recomputed
                hash does not match `content_sha256`.
        """
        key = _content_key(data_type, content_sha256)
        try:
            response = self._client.get_object(self._bucket, key)
            buffer = io.BytesIO(response.read())
        except S3Error as exc:
            raise FileNotFoundError(
                f"No snapshot found at {self._bucket}/{key}"
            ) from exc

        df = pd.read_parquet(buffer)
        actual_hash = canonical_content_sha256(df, data_type)
        if actual_hash != content_sha256:
            raise SnapshotIntegrityError(
                f"Snapshot at {self._bucket}/{key} does not match its "
                f"content-addressed hash: expected {content_sha256}, got "
                f"{actual_hash}."
            )

        logger.info(
            "snapshot_loaded",
            path=f"{self._bucket}/{key}",
            rows=len(df),
            data_type=data_type,
            content_sha256=content_sha256,
        )
        return df

    def load_snapshot_by_manifest(self, manifest, data_type: str) -> pd.DataFrame:
        """Load a snapshot for `data_type` using a `DatasetManifest`'s
        recorded content hash. Preferred over calling `load_snapshot`
        directly with a hand-supplied hash, since the manifest is the
        canonical source of "what hash was this bundle built from."

        Raises:
            FileNotFoundError: if the manifest has no object_path/hash for
                `data_type`, or if no snapshot exists at that content key.
            SnapshotIntegrityError: if the downloaded content does not match
                the manifest's recorded hash.
        """
        content_hash = manifest.content_sha256.get(data_type) if manifest.content_sha256 else None
        if not content_hash:
            raise FileNotFoundError(
                f"Manifest has no recorded content_sha256 for data_type={data_type!r}"
            )
        return self.load_snapshot(data_type, content_hash)

    def list_snapshots(self, data_type: str) -> list[str]:
        """List content-addressed snapshot hashes available for a data_type.

        Pre-03A-1 semantics returned snapshot *dates* (the object key
        encoded a date). Object keys are now content-addressed and carry no
        date, so this returns the sha256 hex digests found under the
        data_type's prefix instead. Human-facing "what's pinned" lookups
        should go through a manifest (`manifests/latest/{strategy_id}.json`
        per section 2.2), not this low-level listing.
        """
        prefix = f"snapshots/{data_type}/sha256/"
        objects = self._client.list_objects(self._bucket, prefix=prefix, recursive=True)
        hashes: list[str] = []
        for obj in objects:
            # Object name pattern: snapshots/{data_type}/sha256/{h2}/{hash}/data.parquet
            parts = obj.object_name.rstrip("/").split("/")
            if len(parts) >= 5 and parts[-1] == "data.parquet":
                hashes.append(parts[-2])
        return sorted(set(hashes))

    # ─── Content-addressing internals ──────────────────────────────────────

    def _object_exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except S3Error:
            return False

    def _verify_existing_object(self, key: str, data_type: str, expected_hash: str) -> bool:
        """Download and recompute the canonical hash of an already-present
        object rather than trusting "key exists" alone (section 2.1)."""
        response = self._client.get_object(self._bucket, key)
        buffer = io.BytesIO(response.read())
        df = pd.read_parquet(buffer)
        return canonical_content_sha256(df, data_type) == expected_hash

    def save_dataset_manifest(self, manifest) -> str:
        """Save a DatasetManifest alongside this snapshot collection."""
        from backtesting.dataset_manifest import save_manifest

        return save_manifest(manifest, self._client, self._bucket)

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
