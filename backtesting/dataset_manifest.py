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

Manifest path convention (03A-1, content-addressed — BUG-038)
---------------------------------------------------------------
    {bucket}/manifests/{manifest_content_sha256}/manifest.json
    e.g. rqis-snapshots/manifests/3fae.../manifest.json

`manifest_content_sha256` is the SHA-256 of the manifest's own canonical JSON
(every field except itself); it is the value passed as MLflow `data_version`
going forward (C7), replacing the old caller-supplied date string. A mutable
advisory pointer, `manifests/latest/{strategy_id}.json`, may point at the
newest manifest for a strategy but is never itself a `data_version`.

Legacy manifests written before 03A-1 used a caller-supplied `version` date
string as both the manifest key and the (mutable) snapshot object keys; those
objects/manifests are left in place read-only and load as before, but must be
flagged `legacy_mutable` (see `scripts/backfill_legacy_manifests.py`) and are
never eligible to be produced by `build_manifest`/`save_manifest` again.

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
    manifest_path = save_manifest(manifest, minio_client, bucket)
    data_version = manifest.manifest_content_sha256  # pass to BacktestLogger.log_run()

    # When loading for a backtest:
    manifest = load_manifest(data_version, minio_client, bucket)
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Optional, Union

import pandas as pd
import structlog
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from data.storage.canonical_hash import canonical_content_sha256
from data.storage.errors import (
    SnapshotIntegrityError,
    SnapshotNotFoundError,
    SnapshotPartialReadError,
)

logger = structlog.get_logger(__name__)


class ManifestBatchLinkageError(ValueError):
    """A manifest referenced an ``eligibility_batch_id``,
    ``membership_import_batch_id``, or ``research_methodology_id`` that does
    not exist (or, for ``membership_import_batch_id``, is not in
    ``published`` status) -- 03A-5, design plan §2.5: "A manifest referencing
    ... that does not exist ... fails to build rather than pinning a bundle
    against an unpublished universe import."
    """

# Data types that participate in a standard backtest bundle.
_BUNDLE_TYPES = ("daily_prices", "alpha_scores", "corporate_actions", "benchmark")

# Column that holds the primary date for each data type.
_DATE_COL = {
    "daily_prices": "date",
    "alpha_scores": "score_date",
    "corporate_actions": "ex_date",
    "benchmark": "date",
}

# Matches the content-addressed key produced by ParquetSnapshots._content_key,
# used to cross-check that an object_path's embedded hash agrees with the
# manifest's independently-computed content_sha256 (section 2.2: "the
# manifest's recorded canonical hash and the object key's hash are the same
# value by construction ... save_manifest checks that equality rather than
# merely asserting it").
_CONTENT_KEY_RE = re.compile(r"/sha256/[0-9a-f]{2}/([0-9a-f]{64})/data\.parquet$")


def _extract_key_hash(object_path: str) -> Optional[str]:
    match = _CONTENT_KEY_RE.search(object_path)
    return match.group(1) if match else None


@dataclass
class DatasetManifest:
    """Complete record of all data sources used in one backtest run."""
    version: str                         # human-readable label, e.g. "2026-06-10"
    created_at: str                      # ISO 8601 UTC timestamp
    git_commit: str                      # HEAD sha or "unknown"
    strategy_id: str
    snapshot_dates: dict[str, str]       # data_type → YYYY-MM-DD
    object_paths: dict[str, str]         # data_type → MinIO bucket/key
    row_counts: dict[str, int]           # data_type → number of rows
    date_ranges: dict[str, list[str]]    # data_type → [min_date, max_date]
    schema_hashes: dict[str, str]        # data_type → sha256[:16] of sorted columns
    # SHA-256 of (score_date, ticker, alpha_score) rows sorted deterministically.
    # Kept for backward compatibility with legacy manifests; content_sha256
    # ["alpha_scores"] is the same value going forward. "" for legacy manifests
    # that predate this field.
    alpha_scores_sha256: str = ""
    # 03A-1: canonical LOGICAL content hash (section 2.1) per data type, for
    # all four bundle types -- generalizes alpha_scores_sha256 to the whole
    # bundle. This is also the hash embedded in each object's content-
    # addressed key.
    content_sha256: dict[str, str] = field(default_factory=dict)
    # 03A-1: secondary, informational SHA-256 of the uploaded parquet bytes
    # per data type. Never used for keys or load-time gating (section 2.1
    # trade-off) -- recorded only so out-of-band byte churn is observable.
    bytes_sha256: dict[str, str] = field(default_factory=dict)
    # 03A-5: FKs linking this bundle to the exact PIT membership/eligibility
    # batches used to build it, plus the optional research methodology that
    # produced its alpha scores. Nullable: legacy bundles pinned before the
    # universe import/eligibility/methodology systems existed (or bundles
    # for which no matching batch was found) leave these unset; build_manifest
    # only fail-closed-validates a batch id that IS supplied (see
    # ManifestBatchLinkageError).
    eligibility_batch_id: Optional[int] = None
    membership_import_batch_id: Optional[int] = None
    research_methodology_id: Optional[int] = None
    # 03A-1: True only for manifests written before content addressing
    # (backfilled by scripts/backfill_legacy_manifests.py), or manifests
    # built under legacy_mutable=True explicitly. False for all manifests
    # produced by this module going forward.
    legacy_mutable: bool = False
    # 03A-1: SHA-256 of this manifest's own canonical JSON (every field
    # except this one). This is the value used as MLflow data_version and as
    # the manifest object key (manifests/{manifest_content_sha256}/manifest.json).
    # "" until save_manifest/build_manifest compute it; legacy manifests use
    # their date-string `version` as their key instead and leave this "".
    manifest_content_sha256: str = ""


def build_manifest(
    version: str,
    strategy_id: str,
    dataframes: dict[str, pd.DataFrame],
    object_paths: dict[str, str],
    snapshot_dates: dict[str, date],
    bytes_sha256: Optional[dict[str, str]] = None,
    eligibility_batch_id: Optional[int] = None,
    membership_import_batch_id: Optional[int] = None,
    research_methodology_id: Optional[int] = None,
    engine: Optional[Union[Engine, str]] = None,
) -> DatasetManifest:
    """Build a DatasetManifest from already-loaded DataFrames.

    Args:
        version: Snapshot version string (YYYY-MM-DD).
        strategy_id: Strategy identifier used to filter alpha_scores.
        dataframes: Mapping of data_type → loaded DataFrame.
        object_paths: Mapping of data_type → MinIO object path.
        snapshot_dates: Mapping of data_type → snapshot date (``date`` object).
        bytes_sha256: Optional mapping of data_type → SHA-256 of the stored
            parquet carrier bytes, as emitted by
            ``ParquetSnapshots.save_snapshot(..., bytes_sha256_out=...)``.
            Informational only (section 2.1 trade-off): recorded on the
            manifest so out-of-band byte churn is observable, but excluded
            from ``manifest_content_sha256`` because it is nondeterministic.
        eligibility_batch_id: Optional FK to
            ``data.universe.models.UniverseEligibilityBatch.id`` (03A-5).
            When supplied, must reference an existing row or building fails
            closed with :class:`ManifestBatchLinkageError`.
        membership_import_batch_id: Optional FK to
            ``data.universe.models.UniverseImportBatch.id`` (03A-5). When
            supplied, must reference a row in ``published`` status (the same
            status ``PITUniverseLookup`` requires) or building fails closed
            with :class:`ManifestBatchLinkageError`.
        research_methodology_id: Optional FK to
            ``data.research.models.ResearchMethodology.id`` (03A-5). When
            supplied, must reference an existing row or building fails
            closed with :class:`ManifestBatchLinkageError`.
        engine: SQLAlchemy engine or connection string used to validate any
            of the three batch/methodology ids above. Required whenever any
            of those ids is supplied (fail-closed: a batch id cannot be
            trusted without checking the DB); ignored if none are supplied.

    Returns:
        DatasetManifest ready to save via save_manifest().

    Raises:
        ManifestBatchLinkageError: an ``eligibility_batch_id`` or
            ``membership_import_batch_id``/``research_methodology_id`` was
            supplied but does not resolve to an existing (and, for
            ``membership_import_batch_id``, published) row.
        ValueError: a batch/methodology id was supplied without ``engine``.
    """
    if (
        eligibility_batch_id is not None
        or membership_import_batch_id is not None
        or research_methodology_id is not None
    ) and engine is None:
        raise ValueError(
            "engine is required to validate eligibility_batch_id/"
            "membership_import_batch_id/research_methodology_id against the "
            "database before building a manifest that links to them (03A-5, "
            "fail-closed: an unvalidated batch id cannot be trusted)."
        )

    if membership_import_batch_id is not None:
        _validate_membership_import_batch(engine, membership_import_batch_id)
    if eligibility_batch_id is not None:
        _validate_eligibility_batch(engine, eligibility_batch_id)
    if research_methodology_id is not None:
        _validate_research_methodology(engine, research_methodology_id)

    row_counts: dict[str, int] = {}
    date_ranges: dict[str, list[str]] = {}
    schema_hashes: dict[str, str] = {}
    content_hashes: dict[str, str] = {}

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
        content_hashes[data_type] = canonical_content_sha256(df, data_type)

        # section 2.2: the manifest's independently-computed content hash and
        # the hash embedded in the object's content-addressed key must be the
        # same value by construction; check that equality rather than merely
        # asserting it.
        object_path = object_paths.get(data_type)
        if object_path:
            key_hash = _extract_key_hash(object_path)
            if key_hash is not None and key_hash != content_hashes[data_type]:
                raise ValueError(
                    f"Content hash mismatch for data_type={data_type!r}: "
                    f"object_path {object_path!r} encodes hash {key_hash}, "
                    f"but the dataframe's canonical content hash is "
                    f"{content_hashes[data_type]}. Refusing to build a "
                    "manifest whose recorded hash disagrees with the object "
                    "it points at."
                )

    alpha_df = dataframes.get("alpha_scores", pd.DataFrame())
    scores_hash = content_hashes.get("alpha_scores") or _alpha_scores_hash(alpha_df)

    manifest = DatasetManifest(
        version=version,
        created_at=datetime.now(timezone.utc).isoformat(),
        git_commit=_git_commit(),
        strategy_id=strategy_id,
        snapshot_dates={k: str(v) for k, v in snapshot_dates.items()},
        object_paths=object_paths,
        row_counts=row_counts,
        date_ranges=date_ranges,
        schema_hashes=schema_hashes,
        alpha_scores_sha256=scores_hash,
        content_sha256=content_hashes,
        bytes_sha256=dict(bytes_sha256) if bytes_sha256 else {},
        eligibility_batch_id=eligibility_batch_id,
        membership_import_batch_id=membership_import_batch_id,
        research_methodology_id=research_methodology_id,
    )
    manifest.manifest_content_sha256 = _manifest_content_sha256(manifest)
    return manifest


def _validate_membership_import_batch(engine: Union[Engine, str], batch_id: int) -> None:
    """Fail closed unless ``batch_id`` references a ``published``
    ``UniverseImportBatch`` (03A-5) -- the same status
    ``PITUniverseLookup`` requires before trusting an import for historical
    research (``data/universe/runtime.py``)."""
    from data.universe.models import UniverseImportBatch  # lazy: avoid import cycle

    if isinstance(engine, str):
        engine = create_engine(engine)
    with Session(engine) as session:
        batch = session.get(UniverseImportBatch, batch_id)
        if batch is None or batch.status != "published":
            raise ManifestBatchLinkageError(
                f"membership_import_batch_id={batch_id} does not reference a "
                "published UniverseImportBatch. Refusing to build a manifest "
                "that pins a bundle against a nonexistent or unpublished "
                "universe import (03A-5, design plan §2.5)."
            )


def _validate_eligibility_batch(engine: Union[Engine, str], batch_id: int) -> None:
    """Fail closed unless ``batch_id`` references an existing
    ``UniverseEligibilityBatch`` row (03A-5). Unlike ``UniverseImportBatch``,
    eligibility batches have no draft/published status column -- the table
    is an append-only log of completed computation runs (§1.2), so mere
    existence is the correct check."""
    from data.universe.models import UniverseEligibilityBatch  # lazy: avoid import cycle

    if isinstance(engine, str):
        engine = create_engine(engine)
    with Session(engine) as session:
        batch = session.get(UniverseEligibilityBatch, batch_id)
        if batch is None:
            raise ManifestBatchLinkageError(
                f"eligibility_batch_id={batch_id} does not reference an "
                "existing UniverseEligibilityBatch row. Refusing to build a "
                "manifest that pins a bundle against a nonexistent "
                "eligibility computation batch (03A-5, design plan §2.5)."
            )


def _validate_research_methodology(engine: Union[Engine, str], methodology_id: int) -> None:
    """Fail closed unless ``methodology_id`` references an existing
    ``ResearchMethodology`` row (03A-5)."""
    from data.research.models import ResearchMethodology  # lazy: avoid import cycle

    if isinstance(engine, str):
        engine = create_engine(engine)
    with Session(engine) as session:
        row = session.get(ResearchMethodology, methodology_id)
        if row is None:
            raise ManifestBatchLinkageError(
                f"research_methodology_id={methodology_id} does not reference "
                "an existing ResearchMethodology row. Refusing to build a "
                "manifest with a dangling methodology link (03A-5)."
            )


# Fields excluded from manifest_content_sha256 because they are provenance/
# labeling metadata, not logical bundle identity: `created_at` and
# `git_commit` vary run-to-run even when the underlying data is unchanged,
# and `version`/`snapshot_dates` are the human-readable date labels section
# 2.1 explicitly demotes to metadata (re-pinning the same content under a
# different --snapshot-date label must still be recognized as the same
# bundle). `legacy_mutable` and `manifest_content_sha256` itself are also
# excluded (identity/labeling flags, not content).
_IDENTITY_EXCLUDED_FIELDS = frozenset(
    {
        "created_at",
        "git_commit",
        "version",
        "snapshot_dates",
        "legacy_mutable",
        "manifest_content_sha256",
        # bytes_sha256 is a NONDETERMINISTIC parquet-byte hash (writer
        # version/footer/compression vary run-to-run). It MUST stay out of
        # the manifest identity hash: including it would give two pins of
        # identical logical data two different manifest_content_sha256
        # values, silently destroying the section 2.5 idempotency guarantee.
        "bytes_sha256",
    }
)


def _manifest_content_sha256(manifest: DatasetManifest) -> str:
    """SHA-256 of the manifest's canonical JSON, excluding
    `manifest_content_sha256` and other provenance/labeling-only fields
    (section 2.2's "same data_version => same logical inputs" guarantee
    requires this hash to be a pure function of the bundle's content, not of
    when/under-which-label it was pinned)."""
    payload = {
        k: v for k, v in asdict(manifest).items() if k not in _IDENTITY_EXCLUDED_FIELDS
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def save_manifest(manifest: DatasetManifest, minio_client, bucket: str) -> str:
    """Serialise and write manifest to MinIO, content-addressed by
    `manifest_content_sha256` (03A-1 -- BUG-038).

    Args:
        manifest: Built by build_manifest().
        minio_client: Minio client instance.
        bucket: Target bucket (e.g. "rqis-snapshots").

    Returns:
        Full MinIO path "bucket/manifests/{manifest_content_sha256}/manifest.json"
        (or, for a manifest explicitly marked `legacy_mutable`, the legacy
        "bucket/manifests/{version}/manifest.json" path).

    Raises:
        ValueError: if `manifest.manifest_content_sha256` is unset/empty for
            a non-legacy manifest (build_manifest always sets it; a manifest
            constructed by hand without going through build_manifest must
            call `_manifest_content_sha256` itself first), or if an object
            already exists at the computed key with *different* bytes
            (should be structurally impossible since the key derives from
            the hash; this is defense against a hash-collision-shaped bug).
    """
    if manifest.legacy_mutable:
        key = f"manifests/{manifest.version}/manifest.json"
    else:
        if not manifest.manifest_content_sha256:
            raise ValueError(
                "manifest.manifest_content_sha256 is unset; build the "
                "manifest via build_manifest() or compute it explicitly "
                "before calling save_manifest()."
            )
        key = f"manifests/{manifest.manifest_content_sha256}/manifest.json"

    payload = json.dumps(asdict(manifest), indent=2).encode()

    # 03A-2 (adversarial-review follow-up): this write-path existence probe
    # must route through the SAME single translation boundary as every read
    # path (data.storage.parquet_snapshots.get_object_bytes), not do a bare
    # `except Exception` around a direct minio get_object. A previous bare
    # except swallowed a transient store-unavailable/access-denied failure as
    # "object doesn't exist yet" and fell through to put_object -- a
    # write-path fail-OPEN that violates the invariant this slice
    # establishes. Only a genuine SnapshotNotFoundError means "not written
    # yet"; every other error (store unavailable, access denied, partial
    # read, integrity) propagates and aborts the save.
    from data.storage.parquet_snapshots import get_object_bytes

    existing_bytes: Optional[bytes] = None
    try:
        existing_bytes = get_object_bytes(minio_client, bucket, key)
    except SnapshotNotFoundError:
        existing_bytes = None

    if existing_bytes is not None and not manifest.legacy_mutable:
        # Identity check, not a raw byte comparison: two manifests built
        # from identical bundle content can still differ byte-for-byte in
        # provenance-only fields (created_at, git_commit, the human
        # version/snapshot_dates label) while sharing the same
        # manifest_content_sha256 -- that IS the identical-content, safe
        # no-op case (section 2.5: re-pinning under a different
        # --snapshot-date label is still a no-op). Only a stored object at
        # this key whose OWN manifest_content_sha256 disagrees is a genuine
        # problem (should be structurally impossible since the key derives
        # from the hash; defense against a hash-collision-shaped bug).
        try:
            existing_manifest = json.loads(existing_bytes)
            existing_identity = existing_manifest.get("manifest_content_sha256")
        except (json.JSONDecodeError, AttributeError):
            existing_identity = None

        if existing_identity == manifest.manifest_content_sha256:
            logger.info("manifest_write_skipped_content_exists", path=f"{bucket}/{key}")
            return f"{bucket}/{key}"
        raise ValueError(
            f"Object already exists at content-addressed manifest key "
            f"{bucket}/{key} whose own manifest_content_sha256 "
            f"({existing_identity!r}) disagrees with the manifest being "
            f"saved ({manifest.manifest_content_sha256!r}). This should be "
            "structurally impossible since the key derives from the hash; "
            "refusing to overwrite."
        )
    elif existing_bytes is not None and manifest.legacy_mutable:
        # legacy_mutable manifests intentionally keep pre-03A-1 overwrite
        # semantics at this key (date-string keyed): always write through.
        pass

    buf = io.BytesIO(payload)
    minio_client.put_object(
        bucket_name=bucket,
        object_name=key,
        data=buf,
        length=len(payload),
        content_type="application/json",
    )
    path = f"{bucket}/{key}"
    logger.info(
        "manifest_saved",
        path=path,
        version=manifest.version,
        manifest_content_sha256=manifest.manifest_content_sha256,
    )
    return path


# A content-addressed manifest version is exactly a SHA-256 hex digest;
# anything else (a `YYYY-MM-DD` date string) is a legacy mutable key.
_MANIFEST_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_malformed_hash_version(version: str) -> bool:
    """BUG-077: a `version` that is 64 characters long but does NOT match
    the canonical lowercase-hex form (`_MANIFEST_HASH_RE`) -- e.g.
    upper/mixed-case hex, or a 64-character non-hex string -- must never be
    silently treated as a legacy date-string version. Only the *length*
    coincidentally matching a real legacy date string is what made the old
    `is_content_addressed = bool(_MANIFEST_HASH_RE.match(version))` check
    unsafe: anything failing the regex fell through to the "legacy,
    unverified" branch with no further scrutiny, including strings that were
    clearly *meant* to be a sha256 hex digest but got mangled (e.g.
    upper-cased by some intermediate system). Genuine legacy versions are
    short `YYYY-MM-DD` date strings (10 characters) and are unaffected by
    this check.
    """
    return len(version) == 64 and not _MANIFEST_HASH_RE.match(version)


def is_manifest_hash_shaped(data_version: str) -> bool:
    """True iff ``data_version`` is exactly 64 lowercase hex characters --
    the shape of a real ``manifest_content_sha256`` (03A-1). Shared by
    ``load_manifest`` (via ``_MANIFEST_HASH_RE``/``_is_malformed_hash_version``
    above) and by ``BacktestLogger.log_run``/``log_walk_forward_run`` (03A-5,
    design plan §2.5's last acceptance test) so both call sites use one
    definition of "hash-shaped" rather than two independently-maintained
    regexes."""
    return bool(_MANIFEST_HASH_RE.match(data_version))


def require_manifest_hash_data_version(data_version: str) -> None:
    """Raise ``ValueError`` unless ``data_version`` is manifest-hash-shaped
    (03A-5). Legacy caller-supplied date strings (e.g. ``"2026-06-14"``) and
    ad hoc test placeholders (``"snapshot-v1"``, ``"v1"``) fail this check by
    design -- a manifest's ``manifest_content_sha256`` (from
    ``build_manifest``/``pin_snapshot.py``) is the only value that should be
    passed as a new run's C7 ``data_version`` going forward."""
    if not is_manifest_hash_shaped(data_version):
        raise ValueError(
            f"data_version {data_version!r} is not a manifest-hash-shaped "
            "data_version (64 lowercase hex characters). 03A-5 requires new "
            "backtest runs to pass a real manifest_content_sha256 -- pin a "
            "bundle via scripts.pin_snapshot and use the printed hash, or "
            "manifest.manifest_content_sha256 directly -- rather than a "
            "legacy date-string or placeholder data_version."
        )


def load_manifest(version: str, minio_client, bucket: str) -> DatasetManifest:
    """Load a manifest from MinIO by its key, verifying integrity for
    content-addressed loads (03A-1 finding-3 fix).

    For manifests produced by this module's `build_manifest`/`save_manifest`
    (03A-1 onward), `version` must be the manifest's `manifest_content_sha256`
    (i.e. the MLflow `data_version` value) — that is the manifest's actual
    object key going forward. Because that hash IS the C7 `data_version` root
    and every leaf dataframe is trusted against the hashes THIS manifest
    records, the manifest itself must be verified: after parsing, its own
    canonical content hash is recomputed and required to equal `version`.
    A tampered or bit-rotted manifest at the expected key therefore fails
    closed with `SnapshotIntegrityError` instead of being trusted blindly
    (mirrors `ParquetSnapshots.load_snapshot`).

    For pre-03A-1 `legacy_mutable` manifests, `version` is still the original
    caller-supplied date string; those are NOT content-addressed and load
    without hash verification. A date-string `version` can never masquerade
    as a verified-immutable load, and a manifest that claims `legacy_mutable`
    while sitting at a content-addressed key is itself an integrity failure.

    Raises:
        SnapshotNotFoundError: if no manifest exists for version (also
            catchable as `FileNotFoundError` for one deprecation cycle --
            see `data.storage.errors`).
        SnapshotStoreUnavailableError: connection/timeout/DNS/TLS failure or
            any other unexpected object-store error.
        SnapshotAccessDeniedError: auth/authorization failure.
        SnapshotPartialReadError: downloaded byte count did not match the
            response's Content-Length, or the JSON payload failed to parse.
        SnapshotIntegrityError: if a content-addressed manifest does not hash
            to its key, or claims legacy_mutable at a content-addressed key.
    """
    # `data.storage.parquet_snapshots.get_object_bytes` is the single
    # translation boundary from `minio.error.S3Error` to the typed error
    # hierarchy (design plan section 4.2); this module must not catch
    # `S3Error` directly. Imported lazily to avoid a module-level circular
    # import between `backtesting.dataset_manifest` and
    # `data.storage.parquet_snapshots` (the latter imports
    # `backtesting.dataset_manifest.save_manifest` inside a method body for
    # the same reason).
    from data.storage.parquet_snapshots import get_object_bytes

    if _is_malformed_hash_version(version):
        raise ValueError(
            f"version {version!r} is 64 characters long but is not "
            "canonical lowercase sha256 hex; refusing to silently load it "
            "as an unverified legacy manifest (BUG-077). If this is meant "
            "to be a content-addressed data_version, it must be exactly 64 "
            "lowercase hex characters ([0-9a-f])."
        )

    key = f"manifests/{version}/manifest.json"
    raw = get_object_bytes(minio_client, bucket, key)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SnapshotPartialReadError(
            f"{bucket}/{key}: downloaded bytes failed to parse as JSON "
            f"({exc})."
        ) from exc
    # Filter to known fields so manifests written by a newer code version
    # (with extra fields) can still be loaded by older code without TypeError.
    known = DatasetManifest.__dataclass_fields__
    manifest = DatasetManifest(**{k: v for k, v in data.items() if k in known})

    is_content_addressed = bool(_MANIFEST_HASH_RE.match(version))
    if is_content_addressed:
        if manifest.legacy_mutable:
            raise SnapshotIntegrityError(
                f"Manifest at content-addressed key {bucket}/{key} claims "
                "legacy_mutable=true; a legacy mutable manifest can never "
                "legitimately live at a content-addressed hash key. Refusing "
                "to trust it as a verified-immutable data_version."
            )
        recomputed = _manifest_content_sha256(manifest)
        if recomputed != version:
            raise SnapshotIntegrityError(
                f"Manifest at {bucket}/{key} does not match its content-"
                f"addressed hash: expected {version}, recomputed {recomputed}. "
                "The manifest has been tampered with or corrupted; refusing "
                "to use it as a C7 data_version."
            )
    return manifest


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _alpha_scores_hash(alpha_scores: pd.DataFrame) -> str:
    """SHA-256 fingerprint of the alpha score values.

    Rows are sorted by (score_date, ticker) before hashing so the result is
    stable regardless of DataFrame construction order.  Uses a canonical
    string encoding — not .tobytes() — so the hash is platform-independent.

    An empty or missing alpha_scores DataFrame returns the SHA-256 of an
    empty byte string, which is a valid sentinel distinct from any real hash.
    """
    required = {"alpha_score", "score_date", "ticker"}
    if alpha_scores.empty or not required.issubset(alpha_scores.columns):
        return hashlib.sha256(b"").hexdigest()

    sorted_df = alpha_scores.sort_values(["score_date", "ticker"])
    rows = (
        sorted_df[["score_date", "ticker", "alpha_score"]]
        .astype(str)
        .apply("|".join, axis=1)
    )
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


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
