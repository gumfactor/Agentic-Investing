"""Tests for ParquetSnapshots (MinIO-backed parquet versioning).

All MinIO calls are mocked so these run without a live object storage service.
"""

from __future__ import annotations

import io
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from data.storage.parquet_snapshots import ParquetSnapshots


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_minio(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Return a mocked Minio client injected into ParquetSnapshots."""
    with patch("data.storage.parquet_snapshots.Minio") as mock_cls:
        mock_client = MagicMock()
        mock_client.bucket_exists.return_value = True
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
    def test_returns_path_string(self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame) -> None:
        path = snapshots.save_snapshot(sample_df, "daily_prices", snapshot_date=date(2024, 1, 5))
        assert isinstance(path, str)
        assert "daily_prices" in path
        assert "2024-01-05" in path

    def test_calls_put_object(self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame) -> None:
        snapshots.save_snapshot(sample_df, "daily_prices", snapshot_date=date(2024, 1, 5))
        assert snapshots._client.put_object.called

    def test_key_format_is_correct(self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame) -> None:
        snapshots.save_snapshot(sample_df, "alpha_scores", snapshot_date=date(2024, 3, 15))
        call_kwargs = snapshots._client.put_object.call_args[1]
        assert call_kwargs["object_name"] == "snapshots/alpha_scores/2024-03-15/data.parquet"

    def test_uses_today_when_no_date_given(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        with patch("data.storage.parquet_snapshots.date") as mock_date:
            mock_date.today.return_value = date(2024, 6, 5)
            snapshots.save_snapshot(sample_df, "daily_prices")
        call_kwargs = snapshots._client.put_object.call_args[1]
        assert "2024-06-05" in call_kwargs["object_name"]

    def test_content_type_is_octet_stream(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        snapshots.save_snapshot(sample_df, "daily_prices", snapshot_date=date(2024, 1, 5))
        call_kwargs = snapshots._client.put_object.call_args[1]
        assert call_kwargs["content_type"] == "application/octet-stream"


# ─── load_snapshot ────────────────────────────────────────────────────────────

class TestLoadSnapshot:
    def _make_parquet_bytes(self, df: pd.DataFrame) -> bytes:
        table = pa.Table.from_pandas(df, preserve_index=False)
        buf = io.BytesIO()
        pq.write_table(table, buf)
        return buf.getvalue()

    def test_returns_dataframe(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        parquet_bytes = self._make_parquet_bytes(sample_df)
        mock_response = MagicMock()
        mock_response.read.return_value = parquet_bytes
        snapshots._client.get_object.return_value = mock_response

        result = snapshots.load_snapshot("daily_prices", date(2024, 1, 5))
        assert isinstance(result, pd.DataFrame)
        assert list(result.columns) == list(sample_df.columns)
        assert len(result) == len(sample_df)

    def test_raises_file_not_found_on_s3_error(self, snapshots: ParquetSnapshots) -> None:
        from minio.error import S3Error
        snapshots._client.get_object.side_effect = S3Error(
            code="NoSuchKey", message="not found",
            resource="", request_id="", host_id="", response=MagicMock()
        )
        with pytest.raises(FileNotFoundError):
            snapshots.load_snapshot("daily_prices", date(2024, 1, 5))

    def test_requests_correct_object_key(
        self, snapshots: ParquetSnapshots, sample_df: pd.DataFrame
    ) -> None:
        parquet_bytes = self._make_parquet_bytes(sample_df)
        mock_response = MagicMock()
        mock_response.read.return_value = parquet_bytes
        snapshots._client.get_object.return_value = mock_response

        snapshots.load_snapshot("daily_prices", date(2024, 3, 10))
        call_args = snapshots._client.get_object.call_args
        assert call_args[0][1] == "snapshots/daily_prices/2024-03-10/data.parquet"


# ─── list_snapshots ───────────────────────────────────────────────────────────

class TestListSnapshots:
    def _make_objects(self, date_strings: list[str]) -> list[MagicMock]:
        objects = []
        for d in date_strings:
            obj = MagicMock()
            obj.object_name = f"snapshots/daily_prices/{d}/"
            objects.append(obj)
        return objects

    def test_returns_dates_sorted_newest_first(self, snapshots: ParquetSnapshots) -> None:
        snapshots._client.list_objects.return_value = self._make_objects(
            ["2024-01-05", "2024-03-01", "2024-02-10"]
        )
        result = snapshots.list_snapshots("daily_prices")
        assert result == [date(2024, 3, 1), date(2024, 2, 10), date(2024, 1, 5)]

    def test_returns_empty_when_no_snapshots(self, snapshots: ParquetSnapshots) -> None:
        snapshots._client.list_objects.return_value = []
        result = snapshots.list_snapshots("daily_prices")
        assert result == []

    def test_skips_objects_with_malformed_date(self, snapshots: ParquetSnapshots) -> None:
        obj1 = MagicMock()
        obj1.object_name = "snapshots/daily_prices/not-a-date/"
        obj2 = MagicMock()
        obj2.object_name = "snapshots/daily_prices/2024-01-05/"
        snapshots._client.list_objects.return_value = [obj1, obj2]

        result = snapshots.list_snapshots("daily_prices")
        assert result == [date(2024, 1, 5)]


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
