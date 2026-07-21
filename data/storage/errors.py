"""Shared object-store / snapshot integrity error types (03A-1/03A-2).

`SnapshotIntegrityError` lives here (rather than in `parquet_snapshots.py`)
so both the leaf-snapshot loader (`data/storage/parquet_snapshots.py`) and the
manifest loader (`backtesting/dataset_manifest.py`) can raise the same type on
a content-hash mismatch without a circular import between those modules.

03A-2 (design plan section 4) adds the rest of the fail-closed object-store
error taxonomy: `SnapshotNotFoundError`, `SnapshotStoreUnavailableError`,
`SnapshotAccessDeniedError`, and `SnapshotPartialReadError`. Together with
`SnapshotIntegrityError`, these are the ONLY exception types a caller should
ever see out of `data.storage.parquet_snapshots.ParquetSnapshots` --
`data/storage/parquet_snapshots.py` is the single translation boundary from
`minio.error.S3Error` (and lower-level connection failures) to this
hierarchy; no other module should catch `minio.error.S3Error` directly (see
`tests/data/storage/test_s3error_containment.py`).

Of this hierarchy, `SnapshotNotFoundError` is the ONLY member that a caller
may treat as "this optional data is absent" -- and only when the caller has
explicitly opted into that behavior (e.g.
`allow_missing_corporate_actions=True`). Every other member always aborts the
run; none of them may be silently swallowed into a default-empty-frame
fallback.
"""

from __future__ import annotations


class SnapshotIntegrityError(Exception):
    """Raised when stored/downloaded content does not match its expected
    canonical content hash.

    Covers, fail-closed:
      - leaf snapshot load (section 2.3): a downloaded object's parsed
        DataFrame does not hash to the value recorded in the manifest (or
        encoded in the object's own content-addressed key);
      - leaf snapshot save (section 2.1): an object already exists at the
        computed content-addressed key, but re-downloading and re-hashing it
        does not match the hash that produced the key;
      - manifest load (section 2.2/2.3): a downloaded manifest at a
        content-addressed key does not hash to that key -- tampering or bit
        rot on the C7 `data_version` root itself.

    Always aborts the caller's run; never treated as "no data."
    """


class SnapshotNotFoundError(FileNotFoundError):
    """Raised when MinIO reports the object/bucket genuinely does not exist
    (S3 error codes `NoSuchKey`/`NoSuchBucket` only).

    This is the ONLY error in the taxonomy that a caller may catch and treat
    as "this optional data type is absent for this run" -- and only when the
    caller has explicitly opted into that behavior (e.g. an
    `allow_missing_corporate_actions=True` flag). A caller that has not
    opted in should let this propagate like any other error.

    Subclasses `FileNotFoundError` as a deprecation-cycle alias (03A-2
    onward): it narrows/replaces the previous blanket
    `except S3Error: raise FileNotFoundError` behavior, and keeping
    `FileNotFoundError` as a real base class means any pre-existing
    `except FileNotFoundError:` call site does not silently stop catching
    genuine not-found errors while it is migrated to
    `except SnapshotNotFoundError:`. New code should catch
    `SnapshotNotFoundError` directly; do not add new
    `except FileNotFoundError` call sites against this module.
    """


class SnapshotStoreUnavailableError(Exception):
    """Raised when the object store itself could not be reached: connection
    refused, timeout, DNS failure, or TLS failure.

    Infrastructure is down; this is never "no data" and always aborts the
    caller's run.
    """


class SnapshotAccessDeniedError(Exception):
    """Raised on a 403-class S3 auth/authorization failure (e.g.
    `AccessDenied`, `InvalidAccessKeyId`, `SignatureDoesNotMatch`).

    A credentials/policy problem is never "no data" and always aborts the
    caller's run.
    """


class SnapshotPartialReadError(Exception):
    """Raised when a downloaded object's byte count does not match its
    reported `Content-Length`, or when the downloaded bytes fail to parse as
    a valid parquet file (footer parse failure).

    A truncated/corrupt transfer is never "no data" and always aborts the
    caller's run.
    """
