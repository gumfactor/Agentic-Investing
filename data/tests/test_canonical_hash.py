"""Tests for canonical LOGICAL content hashing (03A-1, section 2.1).

Determinism under row order, column order, and equivalent dtype
representations is the core guarantee this module provides; every test here
maps directly to a section 2.5 acceptance test.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from data.storage.canonical_hash import EMPTY_CONTENT_SHA256, canonical_content_sha256


@pytest.fixture
def prices_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["AAPL", "MSFT", "AAPL"],
            "date": [date(2024, 1, 3), date(2024, 1, 2), date(2024, 1, 2)],
            "close": [153.5, 400.0, 152.0],
        }
    )


class TestDeterminism:
    def test_row_order_does_not_change_hash(self, prices_df: pd.DataFrame) -> None:
        shuffled = prices_df.sample(frac=1, random_state=7).reset_index(drop=True)
        assert canonical_content_sha256(prices_df, "daily_prices") == canonical_content_sha256(
            shuffled, "daily_prices"
        )

    def test_column_order_does_not_change_hash(self, prices_df: pd.DataFrame) -> None:
        reordered = prices_df[["close", "date", "ticker"]]
        assert canonical_content_sha256(prices_df, "daily_prices") == canonical_content_sha256(
            reordered, "daily_prices"
        )

    def test_parquet_round_trip_does_not_change_hash(self, prices_df: pd.DataFrame) -> None:
        table = pa.Table.from_pandas(prices_df, preserve_index=False)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="snappy")
        buf.seek(0)
        round_tripped = pd.read_parquet(buf)
        assert canonical_content_sha256(prices_df, "daily_prices") == canonical_content_sha256(
            round_tripped, "daily_prices"
        )

    def test_different_writer_metadata_does_not_change_hash(self, prices_df: pd.DataFrame) -> None:
        """Different compression codec simulates writer/version byte
        nondeterminism; the logical hash must be unaffected."""
        table = pa.Table.from_pandas(prices_df, preserve_index=False)
        buf_snappy = io.BytesIO()
        pq.write_table(table, buf_snappy, compression="snappy")
        buf_gzip = io.BytesIO()
        pq.write_table(table, buf_gzip, compression="gzip")

        buf_snappy.seek(0)
        buf_gzip.seek(0)
        assert buf_snappy.getvalue() != buf_gzip.getvalue()  # bytes genuinely differ

        df_snappy = pd.read_parquet(buf_snappy)
        df_gzip = pd.read_parquet(buf_gzip)
        assert canonical_content_sha256(df_snappy, "daily_prices") == canonical_content_sha256(
            df_gzip, "daily_prices"
        )

    def test_changed_value_changes_hash(self, prices_df: pd.DataFrame) -> None:
        changed = prices_df.copy()
        changed.loc[0, "close"] = 999.99
        assert canonical_content_sha256(prices_df, "daily_prices") != canonical_content_sha256(
            changed, "daily_prices"
        )

    def test_empty_dataframe_returns_sentinel(self) -> None:
        assert canonical_content_sha256(pd.DataFrame(), "daily_prices") == EMPTY_CONTENT_SHA256

    def test_none_returns_sentinel(self) -> None:
        assert canonical_content_sha256(None, "daily_prices") == EMPTY_CONTENT_SHA256

    def test_unknown_data_type_falls_back_to_all_columns_sort(self) -> None:
        df = pd.DataFrame({"b": [2, 1], "a": ["y", "x"]})
        shuffled = df.iloc[::-1].reset_index(drop=True)
        assert canonical_content_sha256(df, "some_new_type") == canonical_content_sha256(
            shuffled, "some_new_type"
        )

    def test_different_data_types_are_independent(self, prices_df: pd.DataFrame) -> None:
        # Same dataframe hashed under two data_type labels can differ because
        # the canonical sort key differs -- not asserting equality, just that
        # the function accepts arbitrary data_type strings without raising.
        h1 = canonical_content_sha256(prices_df, "daily_prices")
        h2 = canonical_content_sha256(prices_df, "benchmark")
        assert isinstance(h1, str) and isinstance(h2, str)


class TestNormalizationEdgeCases:
    def test_negative_zero_hashes_same_as_positive_zero(self) -> None:
        """0.0 and -0.0 compare equal but repr differently; they must hash
        identically (P0-2)."""
        pos = pd.DataFrame({"ticker": ["AAPL"], "date": [date(2024, 1, 2)], "x": [0.0]})
        neg = pd.DataFrame({"ticker": ["AAPL"], "date": [date(2024, 1, 2)], "x": [-0.0]})
        # sanity: they are genuinely different repr but equal value
        assert repr(0.0) != repr(-0.0)
        assert canonical_content_sha256(pos, "daily_prices") == canonical_content_sha256(
            neg, "daily_prices"
        )

    def test_computed_negative_zero_matches(self) -> None:
        computed = 0.1 - 0.1  # yields 0.0, but guard against -0.0 arithmetic paths
        df1 = pd.DataFrame({"ticker": ["A"], "date": [date(2024, 1, 2)], "x": [computed]})
        df2 = pd.DataFrame({"ticker": ["A"], "date": [date(2024, 1, 2)], "x": [-0.0]})
        assert canonical_content_sha256(df1, "daily_prices") == canonical_content_sha256(
            df2, "daily_prices"
        )

    def test_decimal_matches_equal_float(self) -> None:
        """NUMERIC(18,6) columns can arrive as Decimal; Decimal('100.500000')
        must hash identically to float 100.5 (P0-4)."""
        dec = pd.DataFrame(
            {"ticker": ["AAPL"], "date": [date(2024, 1, 2)], "close": [Decimal("100.500000")]}
        )
        flt = pd.DataFrame(
            {"ticker": ["AAPL"], "date": [date(2024, 1, 2)], "close": [100.5]}
        )
        assert canonical_content_sha256(dec, "daily_prices") == canonical_content_sha256(
            flt, "daily_prices"
        )

    def test_duplicate_sort_key_order_independent(self) -> None:
        """Rows sharing the full declared sort key must order deterministically
        by their remaining content, not by incidental input order (P0-3)."""
        df = pd.DataFrame(
            {
                "ticker": ["AAPL", "AAPL"],
                "ex_date": [date(2024, 1, 2), date(2024, 1, 2)],
                "action_type": ["dividend", "dividend"],  # identical declared key
                "value": [0.24, 0.99],  # differ only in a non-key column
            }
        )
        shuffled = df.iloc[::-1].reset_index(drop=True)
        assert canonical_content_sha256(df, "corporate_actions") == canonical_content_sha256(
            shuffled, "corporate_actions"
        )


class TestEncodingInjectivity:
    def test_embedded_field_separator_does_not_collide(self) -> None:
        """A cell containing the field separator byte (\\x1f) must not shift
        column boundaries and collide with a different logical frame
        (finding-1). Length-prefixed fields make the encoding injective."""
        sep = "\x1f"
        df1 = pd.DataFrame({"ticker": [f"AAPL{sep}XYZ"], "note": ["foo"]})
        df2 = pd.DataFrame({"ticker": ["XYZ"], "note": [f"foo{sep}AAPL"]})
        assert canonical_content_sha256(df1, "unknown") != canonical_content_sha256(
            df2, "unknown"
        )

    def test_embedded_row_separator_does_not_collide(self) -> None:
        rowsep = "\x1e"
        df1 = pd.DataFrame({"a": [f"x{rowsep}y"], "b": ["z"]})
        df2 = pd.DataFrame({"a": ["x"], "b": [f"y{rowsep}z"]})
        assert canonical_content_sha256(df1, "unknown") != canonical_content_sha256(
            df2, "unknown"
        )

    def test_length_prefix_boundary_case(self) -> None:
        """Values whose length digits could be confused with content must
        still be distinguished (e.g. "1:x" as a literal cell)."""
        df1 = pd.DataFrame({"a": ["1:x"], "b": ["y"]})
        df2 = pd.DataFrame({"a": ["1"], "b": [":xy"]})
        assert canonical_content_sha256(df1, "unknown") != canonical_content_sha256(
            df2, "unknown"
        )


class TestPerDataTypeSortKeys:
    def test_alpha_scores_sorts_by_score_date_ticker(self) -> None:
        df = pd.DataFrame(
            {
                "ticker": ["MSFT", "AAPL"],
                "score_date": [date(2024, 1, 2), date(2024, 1, 2)],
                "alpha_score": [0.5, 0.9],
            }
        )
        reordered = df.iloc[::-1].reset_index(drop=True)
        assert canonical_content_sha256(df, "alpha_scores") == canonical_content_sha256(
            reordered, "alpha_scores"
        )

    def test_corporate_actions_sorts_by_ticker_ex_date_action_type(self) -> None:
        df = pd.DataFrame(
            {
                "ticker": ["AAPL", "AAPL"],
                "ex_date": [date(2024, 1, 2), date(2024, 1, 2)],
                "action_type": ["dividend", "split"],
                "value": [0.24, 4.0],
            }
        )
        reordered = df.iloc[::-1].reset_index(drop=True)
        assert canonical_content_sha256(df, "corporate_actions") == canonical_content_sha256(
            reordered, "corporate_actions"
        )
