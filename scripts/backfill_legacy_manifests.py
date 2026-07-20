"""One-time backfill: flag pre-03A-1 date-keyed manifests as legacy_mutable.

03A-1 (docs/plans/03a-immutable-research-data-design.md section 5.1) leaves
every existing date-keyed snapshot object (snapshots/{data_type}/{date}/...)
and manifest (manifests/{date}/manifest.json) in place, untouched, and never
migrates them into the content-addressed layout automatically -- there is no
way to know whether two same-dated objects from different pin runs represent
the "same" logical content without hashing them anyway, so the legacy layout
is treated as a closed, read-only historical record.

This script is read-only with respect to the actual snapshot/manifest DATA
objects (`snapshots/**`): it never touches them. Its only write is to
overwrite each legacy manifest JSON at its EXISTING date-string key with the
same content plus `legacy_mutable: true` set, so future readers can tell "this
manifest predates the tamper-evidence guarantee" at a glance instead of
re-deriving that from string-matching manifest paths. It is idempotent: a
manifest already flagged `legacy_mutable: true` is left untouched (skipped),
and repeated runs make no further writes.

A manifest is treated as "legacy" (in need of flagging) if and only if it has
no `manifest_content_sha256` -- new content-addressed manifests always have
that field non-empty, while every discovered pre-03A-1 manifest has it "" (or
absent, for manifests predating even the `alpha_scores_sha256`/other newer
fields, filtered out by DatasetManifest's forward-compatible loader).

Usage
-----
    python -m scripts.backfill_legacy_manifests [--dry-run]

Environment
-----------
Requires MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY (used by
ParquetSnapshots) and, indirectly, MINIO_BUCKET_SNAPSHOTS.
"""

from __future__ import annotations

import argparse
import io
import json
from dataclasses import asdict

import structlog

logger = structlog.get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flag pre-03A-1 date-keyed manifests as legacy_mutable."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List manifests that would be flagged without writing anything.",
    )
    return parser.parse_args()


def find_legacy_manifest_keys(minio_client, bucket: str) -> list[str]:
    """List every manifest object key under manifests/ whose immediate
    prefix segment does not look like a 64-hex-char content hash -- i.e.
    every pre-03A-1 date-keyed (or otherwise non-content-addressed) manifest,
    excluding the mutable `manifests/latest/*` advisory pointer namespace."""
    keys: list[str] = []
    for obj in minio_client.list_objects(bucket, prefix="manifests/", recursive=True):
        name = obj.object_name
        if not name.endswith("/manifest.json"):
            continue
        parts = name.split("/")
        if len(parts) != 3:
            continue
        _, version_segment, _ = parts
        if version_segment == "latest":
            continue
        if len(version_segment) == 64 and all(c in "0123456789abcdef" for c in version_segment):
            continue  # already content-addressed
        keys.append(name)
    return sorted(keys)


def backfill_legacy_manifest(minio_client, bucket: str, key: str, *, dry_run: bool) -> bool:
    """Flag one legacy manifest object as `legacy_mutable: true`.

    Returns True if a write happened (or would happen, under --dry-run),
    False if the manifest was already flagged (no-op).
    """
    response = minio_client.get_object(bucket, key)
    data = json.loads(response.read())

    if data.get("legacy_mutable") is True:
        logger.info("legacy_manifest_already_flagged", key=key)
        return False

    data["legacy_mutable"] = True
    # A legacy manifest never had a manifest_content_sha256; make that
    # explicit rather than leaving a stale/absent value.
    data.setdefault("manifest_content_sha256", "")

    if dry_run:
        logger.info("legacy_manifest_would_flag", key=key, dry_run=True)
        return True

    payload = json.dumps(data, indent=2).encode()
    buf = io.BytesIO(payload)
    minio_client.put_object(
        bucket_name=bucket,
        object_name=key,
        data=buf,
        length=len(payload),
        content_type="application/json",
    )
    logger.info("legacy_manifest_flagged", key=key)
    return True


def run_backfill(minio_client, bucket: str, *, dry_run: bool = False) -> dict[str, int]:
    """Flag every legacy date-keyed manifest under `bucket`. Never touches
    the underlying snapshot data objects -- manifest JSON only.

    Returns a summary dict: {"found": N, "flagged": N, "already_flagged": N}.
    """
    keys = find_legacy_manifest_keys(minio_client, bucket)
    flagged = 0
    already = 0
    for key in keys:
        if backfill_legacy_manifest(minio_client, bucket, key, dry_run=dry_run):
            flagged += 1
        else:
            already += 1
    summary = {"found": len(keys), "flagged": flagged, "already_flagged": already}
    logger.info("legacy_manifest_backfill_complete", dry_run=dry_run, **summary)
    return summary


def main() -> None:
    import os

    from dotenv import load_dotenv

    load_dotenv()
    args = _parse_args()

    from data.storage.parquet_snapshots import ParquetSnapshots  # lazy: pulls in minio

    snapshots = ParquetSnapshots()
    summary = run_backfill(snapshots._client, snapshots._bucket, dry_run=args.dry_run)
    print(
        f"Legacy manifests found: {summary['found']}, "
        f"flagged: {summary['flagged']}, "
        f"already flagged: {summary['already_flagged']}"
        + (" (dry-run, no writes)" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()
