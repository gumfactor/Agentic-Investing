"""Tests for DatasetManifest content addressing (03A-1, section 2.2)."""

from __future__ import annotations

import io
import json
from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from backtesting.dataset_manifest import (
    DatasetManifest,
    build_manifest,
    load_manifest,
    save_manifest,
)
from data.storage.canonical_hash import canonical_content_sha256


class _NotFound(Exception):
    pass


def _fake_minio_empty() -> MagicMock:
    """A fake client where every get_object raises (nothing stored yet)."""
    client = MagicMock()
    client.get_object.side_effect = _NotFound("not found")
    return client


def _dataframes() -> dict[str, pd.DataFrame]:
    prices = pd.DataFrame(
        {"ticker": ["AAPL"], "date": [date(2024, 1, 2)], "close": [150.0]}
    )
    alpha = pd.DataFrame(
        {"ticker": ["AAPL"], "score_date": [date(2024, 1, 2)], "alpha_score": [1.0]}
    )
    actions = pd.DataFrame(columns=["ticker", "ex_date", "action_type", "value"])
    benchmark = pd.DataFrame(
        {"ticker": ["SPY"], "date": [date(2024, 1, 2)], "close": [470.0]}
    )
    return {
        "daily_prices": prices,
        "alpha_scores": alpha,
        "corporate_actions": actions,
        "benchmark": benchmark,
    }


def _object_paths(dataframes: dict[str, pd.DataFrame]) -> dict[str, str]:
    paths = {}
    for data_type, df in dataframes.items():
        h = canonical_content_sha256(df, data_type)
        paths[data_type] = f"rqis-snapshots/snapshots/{data_type}/sha256/{h[:2]}/{h}/data.parquet"
    return paths


class TestBuildManifest:
    def test_content_sha256_populated_for_all_four_types(self) -> None:
        dataframes = _dataframes()
        manifest = build_manifest(
            version="2024-01-02",
            strategy_id="v1",
            dataframes=dataframes,
            object_paths=_object_paths(dataframes),
            snapshot_dates={k: date(2024, 1, 2) for k in dataframes},
        )
        assert set(manifest.content_sha256) == set(dataframes)
        for data_type, df in dataframes.items():
            assert manifest.content_sha256[data_type] == canonical_content_sha256(df, data_type)

    def test_manifest_content_sha256_is_set(self) -> None:
        dataframes = _dataframes()
        manifest = build_manifest(
            version="2024-01-02",
            strategy_id="v1",
            dataframes=dataframes,
            object_paths=_object_paths(dataframes),
            snapshot_dates={k: date(2024, 1, 2) for k in dataframes},
        )
        assert manifest.manifest_content_sha256
        assert len(manifest.manifest_content_sha256) == 64

    def test_identical_dataframes_produce_identical_manifest_hash(self) -> None:
        dataframes = _dataframes()
        kwargs = dict(
            version="2024-01-02",
            strategy_id="v1",
            dataframes=dataframes,
            object_paths=_object_paths(dataframes),
            snapshot_dates={k: date(2024, 1, 2) for k in dataframes},
        )
        m1 = build_manifest(**kwargs)
        m2 = build_manifest(**kwargs)
        # created_at is a wall-clock timestamp so the two manifests are not
        # byte-identical, but their content hashes (the part derived from
        # the actual dataframes) must agree.
        assert m1.content_sha256 == m2.content_sha256
        m2.created_at = m1.created_at
        from backtesting.dataset_manifest import _manifest_content_sha256

        assert _manifest_content_sha256(m1) == _manifest_content_sha256(m2)

    def test_changed_row_changes_manifest_hash(self) -> None:
        dataframes = _dataframes()
        m1 = build_manifest(
            version="2024-01-02",
            strategy_id="v1",
            dataframes=dataframes,
            object_paths=_object_paths(dataframes),
            snapshot_dates={k: date(2024, 1, 2) for k in dataframes},
        )

        dataframes2 = _dataframes()
        dataframes2["daily_prices"].loc[0, "close"] = 999.0
        m2 = build_manifest(
            version="2024-01-02",
            strategy_id="v1",
            dataframes=dataframes2,
            object_paths=_object_paths(dataframes2),
            snapshot_dates={k: date(2024, 1, 2) for k in dataframes2},
        )
        assert m1.manifest_content_sha256 != m2.manifest_content_sha256
        assert m1.content_sha256["daily_prices"] != m2.content_sha256["daily_prices"]

    def test_rejects_object_path_hash_mismatch(self) -> None:
        dataframes = _dataframes()
        bad_paths = _object_paths(dataframes)
        bad_paths["daily_prices"] = (
            "rqis-snapshots/snapshots/daily_prices/sha256/ff/" + "f" * 64 + "/data.parquet"
        )
        with pytest.raises(ValueError, match="Content hash mismatch"):
            build_manifest(
                version="2024-01-02",
                strategy_id="v1",
                dataframes=dataframes,
                object_paths=bad_paths,
                snapshot_dates={k: date(2024, 1, 2) for k in dataframes},
            )


class TestSaveLoadManifestRoundTrip:
    def test_save_then_load_round_trips(self) -> None:
        dataframes = _dataframes()
        manifest = build_manifest(
            version="2024-01-02",
            strategy_id="v1",
            dataframes=dataframes,
            object_paths=_object_paths(dataframes),
            snapshot_dates={k: date(2024, 1, 2) for k in dataframes},
        )
        client = _fake_minio_empty()
        stored: dict[str, bytes] = {}

        def _put(bucket_name, object_name, data, length, content_type):
            stored[object_name] = data.read()

        client.put_object.side_effect = _put

        path = save_manifest(manifest, client, "rqis-snapshots")
        assert manifest.manifest_content_sha256 in path

        # Reconfigure get_object to serve what was stored.
        def _get(bucket, key):
            if key not in stored:
                raise _NotFound(key)
            resp = MagicMock()
            resp.read.return_value = stored[key]
            return resp

        client.get_object.side_effect = _get
        # load_manifest only catches minio.error.S3Error, so simulate that
        # specifically for the not-found path via a real S3Error subtype.
        from minio.error import S3Error

        def _get_s3(bucket, key):
            if key not in stored:
                raise S3Error(
                    code="NoSuchKey", message="not found",
                    resource="", request_id="", host_id="", response=MagicMock()
                )
            resp = MagicMock()
            resp.read.return_value = stored[key]
            return resp

        client.get_object.side_effect = _get_s3

        loaded = load_manifest(manifest.manifest_content_sha256, client, "rqis-snapshots")
        assert loaded.manifest_content_sha256 == manifest.manifest_content_sha256
        assert loaded.content_sha256 == manifest.content_sha256
        assert loaded.legacy_mutable is False

    def test_idempotent_resave_writes_nothing_new(self) -> None:
        dataframes = _dataframes()
        manifest = build_manifest(
            version="2024-01-02",
            strategy_id="v1",
            dataframes=dataframes,
            object_paths=_object_paths(dataframes),
            snapshot_dates={k: date(2024, 1, 2) for k in dataframes},
        )
        client = MagicMock()
        payload = json.dumps(
            __import__("dataclasses").asdict(manifest), indent=2
        ).encode()
        resp = MagicMock()
        resp.read.return_value = payload
        client.get_object.return_value = resp

        save_manifest(manifest, client, "rqis-snapshots")
        assert not client.put_object.called

    def test_differing_bytes_at_same_content_key_refused(self) -> None:
        """Should be structurally impossible (key derives from the hash),
        but save_manifest must fail closed rather than silently overwrite."""
        dataframes = _dataframes()
        manifest = build_manifest(
            version="2024-01-02",
            strategy_id="v1",
            dataframes=dataframes,
            object_paths=_object_paths(dataframes),
            snapshot_dates={k: date(2024, 1, 2) for k in dataframes},
        )
        client = MagicMock()
        resp = MagicMock()
        resp.read.return_value = b'{"totally": "different"}'
        client.get_object.return_value = resp

        with pytest.raises(ValueError, match="disagrees"):
            save_manifest(manifest, client, "rqis-snapshots")


class TestLegacyManifest:
    def test_legacy_manifest_missing_new_fields_loads_with_defaults(self) -> None:
        """A manifest JSON written before 03A-1 has none of the new fields;
        load_manifest's forward-compatible field filter must still load it,
        with legacy_mutable defaulting to whatever the stored JSON says (or
        False if wholly absent, pending the backfill script)."""
        legacy_payload = {
            "version": "2026-06-14",
            "created_at": "2026-06-14T00:00:00+00:00",
            "git_commit": "abc123",
            "strategy_id": "v1",
            "snapshot_dates": {"daily_prices": "2026-06-14"},
            "object_paths": {"daily_prices": "rqis-snapshots/snapshots/daily_prices/2026-06-14/data.parquet"},
            "row_counts": {"daily_prices": 10},
            "date_ranges": {},
            "schema_hashes": {},
            "alpha_scores_sha256": "deadbeef",
        }
        client = MagicMock()
        resp = MagicMock()
        resp.read.return_value = json.dumps(legacy_payload).encode()
        client.get_object.return_value = resp

        loaded = load_manifest("2026-06-14", client, "rqis-snapshots")
        assert loaded.version == "2026-06-14"
        assert loaded.manifest_content_sha256 == ""
        assert loaded.content_sha256 == {}
        assert loaded.legacy_mutable is False

    def test_save_manifest_legacy_mutable_uses_date_keyed_path(self) -> None:
        manifest = DatasetManifest(
            version="2026-06-14",
            created_at="2026-06-14T00:00:00+00:00",
            git_commit="abc123",
            strategy_id="v1",
            snapshot_dates={},
            object_paths={},
            row_counts={},
            date_ranges={},
            schema_hashes={},
            legacy_mutable=True,
        )
        client = _fake_minio_empty()
        path = save_manifest(manifest, client, "rqis-snapshots")
        assert path == "rqis-snapshots/manifests/2026-06-14/manifest.json"
