"""Tests for TimescaleWriter.

All DB interactions are mocked so these run without a live TimescaleDB.
The mock verifies that the correct SQL is executed with the correct parameters
and that the upsert/conflict logic is wired up properly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

from data.storage.timescale_writer import TimescaleWriter, _to_decimal_or_none, _to_int_or_none


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_ohlcv_df(n: int = 2) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": f"T{i}",
                "date": date(2024, 1, i + 1),
                "open": Decimal("100"),
                "high": Decimal("105"),
                "low": Decimal("99"),
                "close": Decimal("102"),
                "volume": 1_000_000,
                "source_adj_close": Decimal("101"),
                "source": "yfinance",
            }
            for i in range(n)
        ]
    )


def _make_ca_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "ex_date": date(2024, 2, 1),
                "action_type": "split",
                "value": Decimal("2"),
                "notes": None,
                "source": "yfinance",
            }
        ]
    )


def _make_flags_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "date": date(2024, 1, 2),
                "flag_type": "price_jump",
                "severity": "warning",
                "message": "z=4.2",
            }
        ]
    )


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> TimescaleWriter:
    """Return a TimescaleWriter with a mocked SQLAlchemy engine."""
    with patch("data.storage.timescale_writer.create_engine") as mock_engine_factory:
        mock_engine = MagicMock()
        mock_engine_factory.return_value = mock_engine
        w = TimescaleWriter(database_url="postgresql+psycopg2://fake/fake")
        w._engine = mock_engine
        return w


# ─── upsert_ohlcv ─────────────────────────────────────────────────────────────

class TestUpsertOhlcv:
    def test_returns_zero_for_empty_df(self, writer: TimescaleWriter) -> None:
        assert writer.upsert_ohlcv(pd.DataFrame()) == 0

    def test_raises_on_missing_required_columns(self, writer: TimescaleWriter) -> None:
        df = pd.DataFrame([{"ticker": "AAPL", "date": date(2024, 1, 1)}])
        with pytest.raises(ValueError, match="missing required columns"):
            writer.upsert_ohlcv(df)

    def test_returns_row_count(self, writer: TimescaleWriter) -> None:
        df = _make_ohlcv_df(n=3)
        mock_ctx = MagicMock()
        writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        result = writer.upsert_ohlcv(df)
        assert result == 3

    def test_calls_execute_once_per_batch(self, writer: TimescaleWriter) -> None:
        """With batch_size=5000, 3 rows = 1 execute call."""
        df = _make_ohlcv_df(n=3)
        mock_conn = MagicMock()
        writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        writer.upsert_ohlcv(df)
        assert mock_conn.execute.call_count == 1

    def test_batches_large_dataframe(self, writer: TimescaleWriter) -> None:
        """With batch_size=2, 5 rows = 3 execute calls."""
        small_writer = TimescaleWriter.__new__(TimescaleWriter)
        small_writer._engine = writer._engine
        small_writer._batch_size = 2

        df = _make_ohlcv_df(n=5)
        mock_conn = MagicMock()
        small_writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        small_writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        small_writer.upsert_ohlcv(df)
        assert mock_conn.execute.call_count == 3

    def test_sql_contains_on_conflict(self, writer: TimescaleWriter) -> None:
        df = _make_ohlcv_df(n=1)
        mock_conn = MagicMock()
        writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        writer.upsert_ohlcv(df)

        sql_arg = mock_conn.execute.call_args[0][0]
        assert "ON CONFLICT" in str(sql_arg)
        assert "DO UPDATE" in str(sql_arg)

    def test_none_optional_fields_allowed(self, writer: TimescaleWriter) -> None:
        df = pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "date": date(2024, 1, 1),
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": Decimal("150"),
                    "volume": None,
                    "source_adj_close": None,
                    "source": "yfinance",
                }
            ]
        )
        mock_conn = MagicMock()
        writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        result = writer.upsert_ohlcv(df)
        assert result == 1


# ─── upsert_corporate_actions ─────────────────────────────────────────────────

class TestUpsertCorporateActions:
    def test_returns_zero_for_empty_df(self, writer: TimescaleWriter) -> None:
        assert writer.upsert_corporate_actions(pd.DataFrame()) == 0

    def test_raises_on_missing_columns(self, writer: TimescaleWriter) -> None:
        df = pd.DataFrame([{"ticker": "AAPL"}])
        with pytest.raises(ValueError, match="missing required columns"):
            writer.upsert_corporate_actions(df)

    def test_returns_row_count(self, writer: TimescaleWriter) -> None:
        mock_conn = MagicMock()
        writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        result = writer.upsert_corporate_actions(_make_ca_df())
        assert result == 1

    def test_sql_contains_unique_conflict_target(self, writer: TimescaleWriter) -> None:
        mock_conn = MagicMock()
        writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        writer.upsert_corporate_actions(_make_ca_df())

        sql_arg = mock_conn.execute.call_args[0][0]
        assert "ON CONFLICT" in str(sql_arg)


# ─── write_quality_flags ──────────────────────────────────────────────────────

class TestWriteQualityFlags:
    def test_returns_zero_for_empty_df(self, writer: TimescaleWriter) -> None:
        assert writer.write_quality_flags(pd.DataFrame()) == 0

    def test_returns_row_count(self, writer: TimescaleWriter) -> None:
        mock_conn = MagicMock()
        writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        result = writer.write_quality_flags(_make_flags_df())
        assert result == 1

    def test_sql_uses_do_nothing_on_conflict(self, writer: TimescaleWriter) -> None:
        mock_conn = MagicMock()
        writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        writer.write_quality_flags(_make_flags_df())

        sql_arg = mock_conn.execute.call_args[0][0]
        assert "DO NOTHING" in str(sql_arg)


# ─── log_ingestion ────────────────────────────────────────────────────────────

class TestLogIngestion:
    def test_returns_inserted_id(self, writer: TimescaleWriter) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.scalar_one.return_value = 42
        writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        row_id = writer.log_ingestion(
            source="yfinance",
            data_type="ohlcv",
            status="complete",
            records_written=500,
        )
        assert row_id == 42

    def test_failed_status_passes_error_message(self, writer: TimescaleWriter) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.scalar_one.return_value = 1
        writer._engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        writer.log_ingestion(
            source="yfinance",
            data_type="ohlcv",
            status="failed",
            error_message="Connection timeout",
        )

        params = mock_conn.execute.call_args[0][1]
        assert params["error_message"] == "Connection timeout"
        assert params["status"] == "failed"


# ─── get_latest_ingestion_date ────────────────────────────────────────────────

class TestGetLatestIngestionDate:
    def test_returns_none_when_no_rows(self, writer: TimescaleWriter) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.scalar_one_or_none.return_value = None
        writer._engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        result = writer.get_latest_ingestion_date("yfinance", "ohlcv")
        assert result is None

    def test_returns_date_when_present(self, writer: TimescaleWriter) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.scalar_one_or_none.return_value = date(2024, 6, 1)
        writer._engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        writer._engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        result = writer.get_latest_ingestion_date("yfinance", "ohlcv")
        assert result == date(2024, 6, 1)


# ─── Helper function unit tests ───────────────────────────────────────────────

class TestToDecimalOrNone:
    def test_converts_float(self) -> None:
        assert _to_decimal_or_none(152.5) == Decimal("152.5")

    def test_converts_string(self) -> None:
        assert _to_decimal_or_none("99.99") == Decimal("99.99")

    def test_returns_none_for_none(self) -> None:
        assert _to_decimal_or_none(None) is None

    def test_returns_none_for_nan(self) -> None:
        import math
        assert _to_decimal_or_none(float("nan")) is None

    def test_returns_none_for_inf(self) -> None:
        assert _to_decimal_or_none(float("inf")) is None

    def test_returns_none_for_invalid_string(self) -> None:
        assert _to_decimal_or_none("not_a_number") is None


class TestToIntOrNone:
    def test_converts_float(self) -> None:
        assert _to_int_or_none(1_000_000.0) == 1_000_000

    def test_returns_none_for_none(self) -> None:
        assert _to_int_or_none(None) is None

    def test_returns_none_for_nan(self) -> None:
        import math
        assert _to_int_or_none(float("nan")) is None

    def test_truncates_float(self) -> None:
        assert _to_int_or_none(999.9) == 999
