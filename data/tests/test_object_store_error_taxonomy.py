"""Tests for the 03A-2 fail-closed object-store error taxonomy (design plan
section 4).

Covers:
  - translate_object_store_error's S3 code -> typed exception mapping
    (section 4.1).
  - get_object_bytes's Content-Length partial-read check.
  - ParquetSnapshots.load_snapshot/load_snapshot_legacy translating a
    simulated connection failure / 403 / not-found into the typed hierarchy
    (section 4.3 acceptance tests).
  - SnapshotNotFoundError's FileNotFoundError deprecation-cycle alias.
"""
from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import urllib3.exceptions
from minio.error import S3Error

from data.storage.errors import (
    SnapshotAccessDeniedError,
    SnapshotNotFoundError,
    SnapshotPartialReadError,
    SnapshotStoreUnavailableError,
)
from data.storage.parquet_snapshots import (
    ParquetSnapshots,
    get_object_bytes,
    translate_object_store_error,
)


def _s3_error(code: str) -> S3Error:
    return S3Error(
        code=code, message="boom",
        resource="", request_id="", host_id="", response=MagicMock()
    )


# ─── translate_object_store_error ──────────────────────────────────────────

@pytest.mark.parametrize("code", ["NoSuchKey", "NoSuchBucket"])
def test_translate_not_found_codes(code: str) -> None:
    result = translate_object_store_error(_s3_error(code), "ctx")
    assert isinstance(result, SnapshotNotFoundError)
    assert isinstance(result, FileNotFoundError)  # deprecation-cycle alias


@pytest.mark.parametrize(
    "code", ["AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"]
)
def test_translate_access_denied_codes(code: str) -> None:
    result = translate_object_store_error(_s3_error(code), "ctx")
    assert isinstance(result, SnapshotAccessDeniedError)


def test_translate_other_s3_codes_are_store_unavailable() -> None:
    result = translate_object_store_error(_s3_error("InternalError"), "ctx")
    assert isinstance(result, SnapshotStoreUnavailableError)


def test_translate_connection_refused_is_store_unavailable() -> None:
    exc = urllib3.exceptions.MaxRetryError(pool=MagicMock(), url="http://x")
    result = translate_object_store_error(exc, "ctx")
    assert isinstance(result, SnapshotStoreUnavailableError)


def test_translate_dns_failure_is_store_unavailable() -> None:
    result = translate_object_store_error(socket.gaierror("no such host"), "ctx")
    assert isinstance(result, SnapshotStoreUnavailableError)


def test_translate_unknown_exception_fails_closed_as_store_unavailable() -> None:
    result = translate_object_store_error(RuntimeError("mystery"), "ctx")
    assert isinstance(result, SnapshotStoreUnavailableError)


# ─── get_object_bytes ──────────────────────────────────────────────────────

def _resp(data: bytes, content_length: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = data
    resp.headers = {"Content-Length": content_length} if content_length is not None else {}
    return resp


def test_get_object_bytes_returns_payload_when_length_matches() -> None:
    client = MagicMock()
    client.get_object.return_value = _resp(b"hello", content_length="5")
    assert get_object_bytes(client, "bucket", "key") == b"hello"


def test_get_object_bytes_raises_partial_read_on_length_mismatch() -> None:
    client = MagicMock()
    client.get_object.return_value = _resp(b"hello", content_length="999")
    with pytest.raises(SnapshotPartialReadError, match="truncated/corrupt"):
        get_object_bytes(client, "bucket", "key")


def test_get_object_bytes_no_content_length_header_skips_check() -> None:
    client = MagicMock()
    client.get_object.return_value = _resp(b"hello")
    assert get_object_bytes(client, "bucket", "key") == b"hello"


def test_get_object_bytes_translates_not_found() -> None:
    client = MagicMock()
    client.get_object.side_effect = _s3_error("NoSuchKey")
    with pytest.raises(SnapshotNotFoundError):
        get_object_bytes(client, "bucket", "key")


def test_get_object_bytes_translates_access_denied() -> None:
    client = MagicMock()
    client.get_object.side_effect = _s3_error("AccessDenied")
    with pytest.raises(SnapshotAccessDeniedError):
        get_object_bytes(client, "bucket", "key")


def test_get_object_bytes_translates_connection_failure() -> None:
    client = MagicMock()
    client.get_object.side_effect = ConnectionError("refused")
    with pytest.raises(SnapshotStoreUnavailableError):
        get_object_bytes(client, "bucket", "key")


def test_get_object_bytes_mid_read_incomplete_read_is_partial_and_closes() -> None:
    """P1 read-path leak fix: a failure DURING response.read() (truncated
    body -> http.client.IncompleteRead) must surface as a typed
    SnapshotPartialReadError, never a raw http.client error, and the stream
    must still be closed/released on that path."""
    import http.client

    client = MagicMock()
    resp = MagicMock()
    resp.read.side_effect = http.client.IncompleteRead(partial=b"ab", expected=100)
    client.get_object.return_value = resp

    with pytest.raises(SnapshotPartialReadError):
        get_object_bytes(client, "bucket", "key")
    resp.close.assert_called_once()
    resp.release_conn.assert_called_once()


def test_get_object_bytes_mid_read_protocol_error_wrapping_incomplete_is_partial() -> None:
    """A urllib3 ProtocolError that wraps an IncompleteRead is a truncated
    body -> SnapshotPartialReadError."""
    import http.client

    client = MagicMock()
    resp = MagicMock()
    inner = http.client.IncompleteRead(partial=b"ab", expected=100)
    resp.read.side_effect = urllib3.exceptions.ProtocolError("Connection broken", inner)
    client.get_object.return_value = resp

    with pytest.raises(SnapshotPartialReadError):
        get_object_bytes(client, "bucket", "key")
    resp.close.assert_called_once()
    resp.release_conn.assert_called_once()


def test_get_object_bytes_mid_read_dropped_connection_is_store_unavailable() -> None:
    """A plain ProtocolError (dropped connection, not a truncated body)
    raised mid-read surfaces as SnapshotStoreUnavailableError -- still typed,
    never a raw urllib3 error."""
    client = MagicMock()
    resp = MagicMock()
    resp.read.side_effect = urllib3.exceptions.ProtocolError("Connection aborted")
    client.get_object.return_value = resp

    with pytest.raises(SnapshotStoreUnavailableError):
        get_object_bytes(client, "bucket", "key")
    resp.close.assert_called_once()
    resp.release_conn.assert_called_once()


# ─── ParquetSnapshots.load_snapshot / load_snapshot_legacy end-to-end ──────

@pytest.fixture
def mock_minio(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "k")
    monkeypatch.setenv("MINIO_SECRET_KEY", "s")
    with patch("data.storage.parquet_snapshots.Minio") as mock_cls:
        client = MagicMock()
        client.bucket_exists.return_value = True
        mock_cls.return_value = client
        yield client


def test_load_snapshot_store_unavailable_aborts(mock_minio: MagicMock) -> None:
    """section 4.3: a simulated connection failure raises
    SnapshotStoreUnavailableError, not a silent empty result."""
    snaps = ParquetSnapshots(bucket="rqis-snapshots")
    mock_minio.get_object.side_effect = urllib3.exceptions.MaxRetryError(
        pool=MagicMock(), url="http://x"
    )
    with pytest.raises(SnapshotStoreUnavailableError):
        snaps.load_snapshot("daily_prices", "a" * 64)


def test_load_snapshot_access_denied_aborts(mock_minio: MagicMock) -> None:
    """section 4.3: a simulated 403 raises SnapshotAccessDeniedError."""
    snaps = ParquetSnapshots(bucket="rqis-snapshots")
    mock_minio.get_object.side_effect = _s3_error("AccessDenied")
    with pytest.raises(SnapshotAccessDeniedError):
        snaps.load_snapshot("daily_prices", "a" * 64)


def test_load_snapshot_not_found_is_catchable_as_snapshot_not_found(
    mock_minio: MagicMock,
) -> None:
    snaps = ParquetSnapshots(bucket="rqis-snapshots")
    mock_minio.get_object.side_effect = _s3_error("NoSuchKey")
    with pytest.raises(SnapshotNotFoundError):
        snaps.load_snapshot("daily_prices", "a" * 64)


def test_load_snapshot_not_found_is_also_catchable_as_file_not_found(
    mock_minio: MagicMock,
) -> None:
    """Deprecation-cycle alias: existing `except FileNotFoundError:` callers
    must still catch a genuine not-found."""
    snaps = ParquetSnapshots(bucket="rqis-snapshots")
    mock_minio.get_object.side_effect = _s3_error("NoSuchKey")
    with pytest.raises(FileNotFoundError):
        snaps.load_snapshot("daily_prices", "a" * 64)


def test_load_snapshot_parquet_parse_failure_is_partial_read(
    mock_minio: MagicMock,
) -> None:
    snaps = ParquetSnapshots(bucket="rqis-snapshots")
    mock_minio.get_object.return_value = _resp(b"not a parquet file")
    with pytest.raises(SnapshotPartialReadError):
        snaps.load_snapshot("daily_prices", "a" * 64)
