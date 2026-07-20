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
from typing import Optional

import pandas as pd
import structlog

from data.storage.canonical_hash import canonical_content_sha256

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
    # 03A-5 (not wired in this phase): FKs linking this bundle to the exact
    # PIT membership/eligibility batches used to build it. Nullable/unused
    # until 03A-4/03A-5 land.
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

    Returns:
        DatasetManifest ready to save via save_manifest().
    """
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
    )
    manifest.manifest_content_sha256 = _manifest_content_sha256(manifest)
    return manifest


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

    existing_bytes: Optional[bytes] = None
    try:
        existing = minio_client.get_object(bucket, key)
        existing_bytes = existing.read()
    except Exception:  # noqa: BLE001 - broad: any "not found"-shaped client
        # error means the object doesn't exist yet; fall through to write it.
        # minio.error.S3Error is the real-world case, but tests use a variety
        # of fake-client exception types for "not found".
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


def load_manifest(version: str, minio_client, bucket: str) -> DatasetManifest:
    """Load a manifest from MinIO by its key.

    For manifests produced by this module's `build_manifest`/`save_manifest`
    (03A-1 onward), `version` must be the manifest's `manifest_content_sha256`
    (i.e. the MLflow `data_version` value) — that is the manifest's actual
    object key going forward. For pre-03A-1 `legacy_mutable` manifests,
    `version` is still the original caller-supplied date string.

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
