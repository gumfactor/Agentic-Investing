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

import http.client
import io
import os
import socket
from datetime import date
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import structlog
import urllib3.exceptions
from minio import Minio
from minio.error import S3Error

from data.storage.canonical_hash import bytes_sha256, canonical_content_sha256

# The typed object-store error hierarchy lives in data.storage.errors so both
# this module and backtesting.dataset_manifest can raise/import the same
# types without a circular import. Re-exported here for backward
# compatibility with existing
# `from data.storage.parquet_snapshots import SnapshotIntegrityError` callers.
from data.storage.errors import (
    SnapshotAccessDeniedError,
    SnapshotIntegrityError,
    SnapshotNotFoundError,
    SnapshotPartialReadError,
    SnapshotStoreUnavailableError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BUCKET = os.environ.get("MINIO_BUCKET_SNAPSHOTS", "rqis-snapshots")

__all__ = [
    "ParquetSnapshots",
    "SnapshotIntegrityError",
    "SnapshotNotFoundError",
    "SnapshotStoreUnavailableError",
    "SnapshotAccessDeniedError",
    "SnapshotPartialReadError",
    "translate_object_store_error",
    "get_object_bytes",
]

# S3 error codes that mean "the object/bucket genuinely does not exist" --
# the ONLY condition that may ever become SnapshotNotFoundError. Every other
# S3Error code (auth failures, bucket policy, throttling, internal errors,
# etc.) is treated conservatively as store-unavailable/access-denied so it
# can never be silently swallowed as "no data" (design plan section 4.1).
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchBucket"})
_ACCESS_DENIED_CODES = frozenset(
    {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "Forbidden"}
)

# Lower-level connection failures (MinIO/urllib3 not reachable at all --
# connection refused, timeout, DNS failure, TLS failure) never surface as
# S3Error; they surface as urllib3/socket/OS-level exceptions instead.
_CONNECTION_ERROR_TYPES = (
    urllib3.exceptions.HTTPError,
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    OSError,
)


def translate_object_store_error(exc: Exception, context: str) -> Exception:
    """Translate a raw MinIO/urllib3 exception into the typed, fail-closed
    object-store error hierarchy (data.storage.errors).

    This is THE single translation boundary (design plan section 4.2): no
    other module should catch `minio.error.S3Error` directly. Callers in
    this module (and `backtesting.dataset_manifest`, via `get_object_bytes`)
    call this from an `except` block and raise the returned exception with
    `from exc`.

    Args:
        exc: The caught MinIO/urllib3/OS-level exception.
        context: Human-readable description of the failed operation (e.g.
            the bucket/key), included in the translated error message.

    Returns:
        A `SnapshotNotFoundError`, `SnapshotAccessDeniedError`,
        `SnapshotStoreUnavailableError`, or (for a truncated/incomplete body
        read) `SnapshotPartialReadError` instance. Never returns
        `SnapshotIntegrityError` -- that is raised directly by the caller
        from post-download hash validation, not from this translation step.
    """
    if isinstance(exc, S3Error):
        code = exc.code
        if code in _NOT_FOUND_CODES:
            return SnapshotNotFoundError(f"{context}: object not found ({code}).")
        if code in _ACCESS_DENIED_CODES:
            return SnapshotAccessDeniedError(
                f"{context}: access denied by object store ({code})."
            )
        # Any other S3 error code (throttling, internal server error,
        # bucket-policy denial not covered above, etc.) is conservatively
        # treated as store-unavailable: never "no data".
        return SnapshotStoreUnavailableError(
            f"{context}: object store returned an unexpected error ({code})."
        )
    # A truncated/incomplete response body raised MID-READ surfaces as
    # http.client.IncompleteRead (the low-level marker that fewer bytes
    # arrived than the server promised). Semantically this is a partial
    # transfer, so classify it as SnapshotPartialReadError -- the same type
    # the explicit byte-count/Content-Length check emits -- rather than
    # store-unavailable. urllib3.exceptions.ProtocolError sometimes wraps an
    # IncompleteRead; treat that wrapper as partial too.
    if isinstance(exc, http.client.IncompleteRead) or (
        isinstance(exc, urllib3.exceptions.ProtocolError)
        and _wraps_incomplete_read(exc)
    ):
        return SnapshotPartialReadError(
            f"{context}: response body ended before the full object was read "
            f"({type(exc).__name__}: {exc}); truncated/corrupt transfer."
        )
    if isinstance(exc, _CONNECTION_ERROR_TYPES):
        return SnapshotStoreUnavailableError(
            f"{context}: could not reach object store ({type(exc).__name__}: {exc})."
        )
    # Unknown exception shape: fail closed as store-unavailable rather than
    # letting it escape untyped or be mistaken for "not found".
    return SnapshotStoreUnavailableError(
        f"{context}: unexpected object-store error ({type(exc).__name__}: {exc})."
    )


def _wraps_incomplete_read(exc: BaseException) -> bool:
    """True if a urllib3 ProtocolError was raised from / carries an
    http.client.IncompleteRead (a truncated body), rather than a generic
    dropped-connection ProtocolError."""
    if isinstance(exc.__cause__, http.client.IncompleteRead) or isinstance(
        exc.__context__, http.client.IncompleteRead
    ):
        return True
    # ProtocolError commonly carries the underlying error as a positional arg.
    return any(isinstance(arg, http.client.IncompleteRead) for arg in exc.args)


def get_object_bytes(minio_client: Minio, bucket: str, key: str) -> bytes:
    """Download an object's raw bytes, translating any MinIO/connection
    failure into the typed error hierarchy and verifying the byte count
    against the response's reported `Content-Length` (SnapshotPartialReadError
    on mismatch).

    This is the shared low-level primitive used by every read path in this
    module, and by `backtesting.dataset_manifest.load_manifest` (which reads
    manifest.json objects with the same `minio_client`/`bucket` shape but
    lives outside this module and therefore must not catch `S3Error`
    directly itself).

    Raises:
        SnapshotNotFoundError: object/bucket does not exist.
        SnapshotAccessDeniedError: auth/authorization failure.
        SnapshotStoreUnavailableError: connection/timeout/DNS/TLS failure or
            any other unexpected object-store error (including a
            dropped-connection ProtocolError raised mid-read).
        SnapshotPartialReadError: downloaded byte count does not match the
            response's reported Content-Length, or the body read failed
            mid-transfer with a truncated-body marker
            (http.client.IncompleteRead / a ProtocolError wrapping one).
    """
    context = f"{bucket}/{key}"
    try:
        response = minio_client.get_object(bucket, key)
    except Exception as exc:  # noqa: BLE001 - translated immediately below
        raise translate_object_store_error(exc, context) from exc

    try:
        try:
            data = response.read()
        except Exception as exc:  # noqa: BLE001 - translated immediately below
            # A failure DURING the body read (truncated body / dropped
            # connection mid-transfer -- http.client.IncompleteRead,
            # urllib3 ProtocolError, socket errors) must surface as a typed,
            # fail-closed exception like every other object-store failure,
            # never as a raw urllib3/http.client error escaping the module
            # boundary (that leak is exactly the BUG-039 class this slice
            # eliminates).
            raise translate_object_store_error(exc, context) from exc
        headers = getattr(response, "headers", None)
        expected_length = headers.get("Content-Length") if headers else None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        release_conn = getattr(response, "release_conn", None)
        if callable(release_conn):
            release_conn()

    # Real MinIO responses report Content-Length as a header string (or
    # plain int for a hand-built fake). Anything else -- including a header
    # store that itself yields a non-numeric-string placeholder -- is
    # treated as "no reported length" rather than risking a false-positive
    # SnapshotPartialReadError against a value that was never a real byte
    # count to begin with.
    if isinstance(expected_length, (str, bytes, int)):
        try:
            expected_bytes = int(expected_length)
        except (TypeError, ValueError):
            expected_bytes = None
        if expected_bytes is not None and len(data) != expected_bytes:
            raise SnapshotPartialReadError(
                f"{context}: read {len(data)} bytes but Content-Length "
                f"reported {expected_bytes}; truncated/corrupt transfer."
            )

    return data


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
        *,
        bytes_sha256_out: Optional[dict[str, str]] = None,
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
            bytes_sha256_out: Optional caller-supplied dict. When provided,
                            the SHA-256 of the actually-stored parquet bytes
                            (the freshly-uploaded payload, or the existing
                            object's bytes on an idempotent skip) is recorded
                            under `data_type`. This is the secondary,
                            INFORMATIONAL byte hash (section 2.1 trade-off):
                            it is nondeterministic across writer versions and
                            must never be used as a key or a load-time gate.

        Returns:
            The MinIO object path (bucket/key) — store this (or the
            enclosing manifest's manifest_content_sha256) as the data_version
            in MLflow to satisfy C7 (pinned data snapshot).
        """
        content_hash = canonical_content_sha256(df, data_type)
        key = _content_key(data_type, content_hash)
        path = f"{self._bucket}/{key}"

        if self._object_exists(key):
            existing_bytes = self._read_object_bytes(key)
            if canonical_content_sha256(pd.read_parquet(io.BytesIO(existing_bytes)), data_type) == content_hash:
                if bytes_sha256_out is not None:
                    bytes_sha256_out[data_type] = bytes_sha256(existing_bytes)
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
        payload = buffer.getvalue()
        if bytes_sha256_out is not None:
            bytes_sha256_out[data_type] = bytes_sha256(payload)
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
            SnapshotNotFoundError: if no snapshot exists at that content key
                (also catchable as `FileNotFoundError` for one deprecation
                cycle -- see `data.storage.errors`).
            SnapshotStoreUnavailableError: connection/timeout/DNS/TLS failure
                or any other unexpected object-store error.
            SnapshotAccessDeniedError: auth/authorization failure.
            SnapshotPartialReadError: downloaded byte count did not match the
                response's Content-Length, or the parquet footer failed to
                parse.
            SnapshotIntegrityError: if the downloaded content's recomputed
                hash does not match `content_sha256`.
        """
        key = _content_key(data_type, content_sha256)
        data = get_object_bytes(self._client, self._bucket, key)

        try:
            df = pd.read_parquet(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001 - any parquet parse failure
            # is a partial/corrupt read, regardless of the underlying
            # pyarrow/pandas exception type raised.
            raise SnapshotPartialReadError(
                f"{self._bucket}/{key}: downloaded bytes failed to parse as "
                f"parquet ({type(exc).__name__}: {exc})."
            ) from exc

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
            SnapshotNotFoundError: if the manifest has no object_path/hash
                for `data_type`, or if no snapshot exists at that content
                key.
            SnapshotStoreUnavailableError / SnapshotAccessDeniedError /
                SnapshotPartialReadError: see `load_snapshot`.
            SnapshotIntegrityError: if the downloaded content does not match
                the manifest's recorded hash.
        """
        content_hash = manifest.content_sha256.get(data_type) if manifest.content_sha256 else None
        if not content_hash:
            raise SnapshotNotFoundError(
                f"Manifest has no recorded content_sha256 for data_type={data_type!r}"
            )
        return self.load_snapshot(data_type, content_hash)

    def load_snapshot_legacy(self, data_type: str, snapshot_date: date) -> pd.DataFrame:
        """Read a pre-03A-1 date-keyed snapshot object.

        03A-1 content-addresses new snapshot objects, but pre-existing
        date-keyed objects (`snapshots/{data_type}/{YYYY-MM-DD}/data.parquet`)
        are retained read-only (design section 5.1). Scripts that still
        consume those legacy objects by date -- `backfill_momentum_scores.py`,
        `audit_pit_safety.py` -- use this method rather than `load_snapshot`,
        whose second argument is now a content hash. No content-hash
        verification is performed because legacy objects were written before
        any per-object logical hash existed.

        Raises:
            SnapshotNotFoundError: if no legacy snapshot exists for that date
                (also catchable as `FileNotFoundError` for one deprecation
                cycle -- see `data.storage.errors`).
            SnapshotStoreUnavailableError: connection/timeout/DNS/TLS failure
                or any other unexpected object-store error.
            SnapshotAccessDeniedError: auth/authorization failure.
            SnapshotPartialReadError: downloaded byte count did not match the
                response's Content-Length, or the parquet footer failed to
                parse.
        """
        key = f"snapshots/{data_type}/{snapshot_date}/data.parquet"
        data = get_object_bytes(self._client, self._bucket, key)

        try:
            df = pd.read_parquet(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001 - any parquet parse failure
            # is a partial/corrupt read, regardless of the underlying
            # pyarrow/pandas exception type raised.
            raise SnapshotPartialReadError(
                f"{self._bucket}/{key}: downloaded bytes failed to parse as "
                f"parquet ({type(exc).__name__}: {exc})."
            ) from exc

        logger.info(
            "snapshot_loaded_legacy",
            path=f"{self._bucket}/{key}",
            rows=len(df),
            data_type=data_type,
            snapshot_date=str(snapshot_date),
        )
        return df

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
        """Return whether an object exists at `key`.

        Only a genuine not-found is treated as "does not exist" (returns
        False); any other failure (store unreachable, auth denied, etc.) is
        translated and raised rather than silently reported as absent --
        this method feeds `save_snapshot`'s idempotent-skip decision, and a
        transient infra failure must never be mistaken for "safe to write."
        """
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except Exception as exc:  # noqa: BLE001 - translated immediately below
            translated = translate_object_store_error(exc, f"{self._bucket}/{key}")
            if isinstance(translated, SnapshotNotFoundError):
                return False
            raise translated from exc

    def _read_object_bytes(self, key: str) -> bytes:
        """Download the raw bytes of an already-present object, so its
        canonical hash can be recomputed (verification, not trusting "key
        exists" alone -- section 2.1) and its informational bytes hash
        recorded."""
        return get_object_bytes(self._client, self._bucket, key)

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
