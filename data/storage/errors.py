"""Shared object-store / snapshot integrity error types (03A-1).

`SnapshotIntegrityError` lives here (rather than in `parquet_snapshots.py`)
so both the leaf-snapshot loader (`data/storage/parquet_snapshots.py`) and the
manifest loader (`backtesting/dataset_manifest.py`) can raise the same type on
a content-hash mismatch without a circular import between those modules.

The full fail-closed object-store error taxonomy (SnapshotStoreUnavailable
Error, SnapshotAccessDeniedError, SnapshotPartialReadError, and narrowing
`FileNotFoundError` to a proper `SnapshotNotFoundError`) is 03A-2's scope;
this module currently carries only the content-integrity error needed for the
load-time tamper checks.
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
    """
