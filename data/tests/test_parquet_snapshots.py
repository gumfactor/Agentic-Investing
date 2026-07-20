"""Tests for ParquetSnapshots (MinIO-backed, content-addressed parquet
versioning -- 03A-1).

All MinIO calls are mocked so these run without a live object storage
service. `mock_minio.stat_object`/`get_object` default to "not found" so
save_snapshot's existence check exercises the upload path unless a test
explicitly configures an existing object.
"""

from __future__ import annotations

import io
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from minio.error import S3Error

from data.storage.canonical_hash import canonical_content_sha256
from data.storage.parquet_snapshots import ParquetSnapshots, SnapshotIntegrityError


def _s3_not_found() -> S3Error:
    return S3Error(
        code="NoSuchKey", message="not found",
        resource="", request_id="", host_id="", response=MagicMock()
    )


def _parquet_bytes(df: pd.DataFrame) -> bytes:
    table = pa.Table.from_pandas(df, preserve_index=False)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_minio(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Return a mocked Minio client injected into ParquetSnapshots."""
    with patch("data.storage.parquet_snapshots.Minio") as mock_cls:
        mock_client = MagicMock()
        mock_client.bucket_exists.return_value = True
        mock_client.stat_object.side_effect = _s3_not_found()
        mock_cls.return_value = mock_client
        yield mock_client


@pytest.fixture
def snapshots(mock_minio: MagicMock, monkeypatch: pytest.MonkeyPatch) -> ParquetSnapshots:
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "test_key")
    monkeypatch.setenv("MINIO_SECRET_KEY", "test_secret")
    with patch("data.storage.parquet_snapshots.Minio") as mock_cls:
        mock_cls.return_value = mock_minio
        s = ParquetSnapshots(
            endpoint="localhost:9000",
            access_key="test_key",
            secret_key="test_secret",
            bucket="test-bucket",
        )
        s._client = mock_minio
        return s


@pytest.fixture
def sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["AAPL", "MSFT"],
            "date": [date(2024, 1, 2), date(2024, 1, 2)],
            "close": [152.0, 400.0],
        }
    )


# ─── save_snapshot ────────────────────────────────────────────────────────────

class TestSaveSnapshot:
    def test_returns_content_addressed_path(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        expected_hash = canonical_content_sha256(sample_df, "daily_prices")
        path = snapshots.save_snapshot(sample_df, "daily_prices", snapshot_date=date(2024, 1, 5))
        assert isinstance(path, str)
        assert "daily_prices" in path
        assert "sha256" in path
        assert expected_hash in path
        # snapshot_date is metadata only, no longer part of the key.
        assert "2024-01-05" not in path

    def test_calls_put_object_when_absent(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        snapshots.save_snapshot(sample_df, "daily_prices", snapshot_date=date(2024, 1, 5))
        assert snapshots._client.put_object.called

    def test_key_format_is_hash_based(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        content_hash = canonical_content_sha256(sample_df, "alpha_scores")
        snapshots.save_snapshot(sample_df, "alpha_scores", snapshot_date=date(2024, 3, 15))
        call_kwargs = snapshots._client.put_object.call_args[1]
        assert call_kwargs["object_name"] == (
            f"snapshots/alpha_scores/sha256/{content_hash[:2]}/{content_hash}/data.parquet"
        )

    def test_content_type_is_octet_stream(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        snapshots.save_snapshot(sample_df, "daily_prices", snapshot_date=date(2024, 1, 5))
        call_kwargs = snapshots._client.put_object.call_args[1]
        assert call_kwargs["content_type"] == "application/octet-stream"

    def test_identical_content_is_idempotent_no_op(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        """Re-saving identical logical content -- even when the parquet
        bytes differ (forced here via a different compression codec, i.e.
        writer nondeterminism) -- skips the upload once an object is already
        verified present at that content hash's key (section 2.5)."""
        content_hash = canonical_content_sha256(sample_df, "daily_prices")
        key = f"snapshots/daily_prices/sha256/{content_hash[:2]}/{content_hash}/data.parquet"

        # First save: object absent -> uploads.
        snapshots.save_snapshot(sample_df, "daily_prices")
        assert snapshots._client.put_object.call_count == 1

        # Second save: simulate the object now existing, encoded with a
        # DIFFERENT parquet writer configuration (byte-nondeterminism), but
        # logically identical values.
        snapshots._client.stat_object.side_effect = None
        snapshots._client.stat_object.return_value = MagicMock()
        table = pa.Table.from_pandas(sample_df, preserve_index=False)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="gzip")  # different codec than save_snapshot uses
        buf.seek(0)
        mock_response = MagicMock()
        mock_response.read.return_value = buf.getvalue()
        snapshots._client.get_object.return_value = mock_response

        snapshots.save_snapshot(sample_df, "daily_prices")
        assert snapshots._client.put_object.call_count == 1  # no new write

    def test_verifies_existing_object_not_just_key_presence(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        """A key that exists but whose content doesn't actually hash to the
        computed key (corrupted/partial prior upload) must fail closed, not
        be silently trusted as a valid skip."""
        content_hash = canonical_content_sha256(sample_df, "daily_prices")
        snapshots._client.stat_object.side_effect = None
        snapshots._client.stat_object.return_value = MagicMock()

        other_df = sample_df.copy()
        other_df.loc[0, "close"] = 1.0
        mock_response = MagicMock()
        mock_response.read.return_value = _parquet_bytes(other_df)
        snapshots._client.get_object.return_value = mock_response

        with pytest.raises(SnapshotIntegrityError):
            snapshots.save_snapshot(sample_df, "daily_prices")

    def test_changed_row_produces_new_key_and_write(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        changed_df = sample_df.copy()
        changed_df.loc[0, "close"] = 999.0

        path1 = snapshots.save_snapshot(sample_df, "daily_prices")
        path2 = snapshots.save_snapshot(changed_df, "daily_prices")

        assert path1 != path2
        assert snapshots._client.put_object.call_count == 2


# ─── load_snapshot ────────────────────────────────────────────────────────────

class TestLoadSnapshot:
    def test_returns_dataframe(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        content_hash = canonical_content_sha256(sample_df, "daily_prices")
        mock_response = MagicMock()
        mock_response.read.return_value = _parquet_bytes(sample_df)
        snapshots._client.get_object.return_value = mock_response

        result = snapshots.load_snapshot("daily_prices", content_hash)
        assert isinstance(result, pd.DataFrame)
        assert list(result.columns) == list(sample_df.columns)
        assert len(result) == len(sample_df)

    def test_raises_file_not_found_on_s3_error(self, snapshots: ParquetSnapshots) -> None:
        snapshots._client.get_object.side_effect = _s3_not_found()
        with pytest.raises(FileNotFoundError):
            snapshots.load_snapshot("daily_prices", "0" * 64)

    def test_requests_correct_object_key(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        content_hash = canonical_content_sha256(sample_df, "daily_prices")
        mock_response = MagicMock()
        mock_response.read.return_value = _parquet_bytes(sample_df)
        snapshots._client.get_object.return_value = mock_response

        snapshots.load_snapshot("daily_prices", content_hash)
        call_args = snapshots._client.get_object.call_args
        assert call_args[0][1] == (
            f"snapshots/daily_prices/sha256/{content_hash[:2]}/{content_hash}/data.parquet"
        )

    def test_corrupted_content_raises_integrity_error(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        """Object stored under a given hash's key, but its parsed content
        does not actually hash to that value (simulated tampering)."""
        content_hash = canonical_content_sha256(sample_df, "daily_prices")
        tampered_df = sample_df.copy()
        tampered_df.loc[0, "close"] = 1.0
        mock_response = MagicMock()
        mock_response.read.return_value = _parquet_bytes(tampered_df)
        snapshots._client.get_object.return_value = mock_response

        with pytest.raises(SnapshotIntegrityError):
            snapshots.load_snapshot("daily_prices", content_hash)

    def test_byte_different_logically_equal_reencoding_loads_successfully(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        """Section 2.1 trade-off: a byte-different but logically-equal
        re-encoding (different compression codec) is not flagged."""
        content_hash = canonical_content_sha256(sample_df, "daily_prices")
        table = pa.Table.from_pandas(sample_df, preserve_index=False)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="gzip")
        buf.seek(0)
        mock_response = MagicMock()
        mock_response.read.return_value = buf.getvalue()
        snapshots._client.get_object.return_value = mock_response

        result = snapshots.load_snapshot("daily_prices", content_hash)
        assert len(result) == len(sample_df)


# ─── list_snapshots ───────────────────────────────────────────────────────────

class TestListSnapshots:
    def _make_objects(self, hashes: list[str]) -> list[MagicMock]:
        objects = []
        for h in hashes:
            obj = MagicMock()
            obj.object_name = f"snapshots/daily_prices/sha256/{h[:2]}/{h}/data.parquet"
            objects.append(obj)
        return objects

    def test_returns_hashes(self, snapshots: ParquetSnapshots) -> None:
        h1 = "a" * 64
        h2 = "b" * 64
        snapshots._client.list_objects.return_value = self._make_objects([h1, h2])
        result = snapshots.list_snapshots("daily_prices")
        assert result == sorted([h1, h2])

    def test_returns_empty_when_no_snapshots(self, snapshots: ParquetSnapshots) -> None:
        snapshots._client.list_objects.return_value = []
        result = snapshots.list_snapshots("daily_prices")
        assert result == []


# ─── save_raw_response ────────────────────────────────────────────────────────

class TestSaveRawResponse:
    def test_stores_to_raw_bucket(
        self, snapshots: ParquetSnapshots, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINIO_BUCKET_RAW", "rqis-raw")
        snapshots.save_raw_response(
            data=b'{"test": "data"}',
            source="yfinance",
            data_type="ohlcv",
            batch_id="abc-123",
        )
        call_kwargs = snapshots._client.put_object.call_args[1]
        assert call_kwargs["bucket_name"] == "rqis-raw"
        assert "yfinance" in call_kwargs["object_name"]
        assert "ohlcv" in call_kwargs["object_name"]
        assert "abc-123" in call_kwargs["object_name"]


# ─── Bucket creation ──────────────────────────────────────────────────────────

class TestEnsureBucket:
    def test_creates_bucket_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch("data.storage.parquet_snapshots.Minio") as mock_cls:
            mock_client = MagicMock()
            mock_client.bucket_exists.return_value = False
            mock_cls.return_value = mock_client

            ParquetSnapshots(
                endpoint="localhost:9000",
                access_key="k",
                secret_key="s",
                bucket="new-bucket",
            )
            assert mock_client.make_bucket.called

    def test_does_not_recreate_existing_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch("data.storage.parquet_snapshots.Minio") as mock_cls:
            mock_client = MagicMock()
            mock_client.bucket_exists.return_value = True
            mock_cls.return_value = mock_client

            ParquetSnapshots(
                endpoint="localhost:9000",
                access_key="k",
                secret_key="s",
                bucket="existing-bucket",
            )
            assert not mock_client.make_bucket.called
