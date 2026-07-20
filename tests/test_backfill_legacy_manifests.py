"""Tests for scripts/backfill_legacy_manifests.py (03A-1, section 5.1).

Read-only with respect to the underlying snapshot DATA objects: the script
only ever rewrites manifest JSON, at its existing key, to add
`legacy_mutable: true`.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from scripts.backfill_legacy_manifests import (
    backfill_legacy_manifest,
    find_legacy_manifest_keys,
    run_backfill,
)


def _obj(name: str) -> MagicMock:
    m = MagicMock()
    m.object_name = name
    return m


def test_find_legacy_manifest_keys_excludes_content_addressed_and_pointer() -> None:
    content_hash = "a" * 64
    client = MagicMock()
    client.list_objects.return_value = [
        _obj("manifests/2026-06-14/manifest.json"),      # legacy date-keyed
        _obj(f"manifests/{content_hash}/manifest.json"),  # content-addressed, skip
        _obj("manifests/latest/v1.json"),                 # mutable pointer, skip
    ]
    keys = find_legacy_manifest_keys(client, "rqis-snapshots")
    assert keys == ["manifests/2026-06-14/manifest.json"]


def test_backfill_legacy_manifest_sets_flag_and_writes() -> None:
    client = MagicMock()
    resp = MagicMock()
    resp.read.return_value = json.dumps({"version": "2026-06-14", "strategy_id": "v1"}).encode()
    client.get_object.return_value = resp

    changed = backfill_legacy_manifest(
        client, "rqis-snapshots", "manifests/2026-06-14/manifest.json", dry_run=False
    )
    assert changed is True
    assert client.put_object.called
    written = json.loads(client.put_object.call_args.kwargs["data"].getvalue())
    assert written["legacy_mutable"] is True
    assert written["manifest_content_sha256"] == ""
    # Original fields preserved, not clobbered.
    assert written["strategy_id"] == "v1"


def test_backfill_legacy_manifest_already_flagged_is_no_op() -> None:
    client = MagicMock()
    resp = MagicMock()
    resp.read.return_value = json.dumps(
        {"version": "2026-06-14", "legacy_mutable": True}
    ).encode()
    client.get_object.return_value = resp

    changed = backfill_legacy_manifest(
        client, "rqis-snapshots", "manifests/2026-06-14/manifest.json", dry_run=False
    )
    assert changed is False
    assert not client.put_object.called


def test_backfill_legacy_manifest_dry_run_writes_nothing() -> None:
    client = MagicMock()
    resp = MagicMock()
    resp.read.return_value = json.dumps({"version": "2026-06-14"}).encode()
    client.get_object.return_value = resp

    changed = backfill_legacy_manifest(
        client, "rqis-snapshots", "manifests/2026-06-14/manifest.json", dry_run=True
    )
    assert changed is True
    assert not client.put_object.called


def test_run_backfill_never_touches_snapshot_data_objects() -> None:
    """Only manifests/** objects are listed/read/written; snapshots/** is
    never referenced by this script."""
    content_hash = "b" * 64
    client = MagicMock()
    client.list_objects.return_value = [
        _obj("manifests/2026-06-14/manifest.json"),
        _obj(f"manifests/{content_hash}/manifest.json"),
    ]
    resp = MagicMock()
    resp.read.return_value = json.dumps({"version": "2026-06-14"}).encode()
    client.get_object.return_value = resp

    summary = run_backfill(client, "rqis-snapshots", dry_run=False)

    assert summary == {"found": 1, "flagged": 1, "already_flagged": 0}
    list_prefixes = [c.kwargs.get("prefix", c.args[1] if len(c.args) > 1 else None)
                      for c in client.list_objects.call_args_list]
    assert all(p is None or p.startswith("manifests/") for p in list_prefixes)
    for call in client.get_object.call_args_list:
        key = call.args[1] if len(call.args) > 1 else call.kwargs.get("object_name")
        assert key.startswith("manifests/")
    for call in client.put_object.call_args_list:
        assert call.kwargs["object_name"].startswith("manifests/")
