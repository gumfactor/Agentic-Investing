"""Tests for the PIT eligibility-attribute batch job (03A-4b, Phase B of
BUG-078): adv_usd_20d/price_usd computation+write, security_type
hand-curated backfill, and the coverage report.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from data.universe.eligibility_batch import (
    EmptyBatchError,
    SecurityTypeCurationEntry,
    SecurityTypeCurationError,
    build_security_type_rows,
    compute_price_eligibility_rows,
    eligibility_coverage_report,
    load_membership_intervals,
    write_price_eligibility_batch,
    write_security_type_batch,
)
from data.universe.import_pipeline import run_import
from data.universe.models import Base, UniverseEligibilityAttribute, UniverseEligibilityBatch
from data.universe.providers.fixture_provider import (
    FIXTURE_COVERAGE_START,
    FIXTURE_UNIVERSE_ID,
    FixtureSP500Provider,
)


@pytest.fixture
def engine(tmp_path: Path):
    return create_engine(f"sqlite:///{tmp_path / 'eligibility_batch.db'}", future=True)


@pytest.fixture
def published_universe(engine):
    """Publish the fixture universe import into `engine` and return its id."""
    run_import(
        FixtureSP500Provider(),
        engine=engine,
        artifact_root=Path.cwd() / "_unused_artifacts_test",
        coverage_start=FIXTURE_COVERAGE_START,
    )
    return FIXTURE_UNIVERSE_ID


def _make_prices(tickers: list[str], dates: list[date], base: float = 100.0) -> pd.DataFrame:
    rows = []
    for t_idx, t in enumerate(tickers):
        level = base + t_idx * 10
        for d_idx, d in enumerate(dates):
            level *= 1.001
            rows.append(
                {
                    "ticker": t,
                    "date": d,
                    "close": round(level, 4),
                    "volume": 1_000_000 + d_idx * 1000,
                }
            )
    return pd.DataFrame(rows)


def _trading_dates(start: date, n: int) -> list[date]:
    from data.universe.calendar import is_trading_session

    out: list[date] = []
    d = start
    while len(out) < n:
        if is_trading_session(d):
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


# ─── compute_price_eligibility_rows ────────────────────────────────────────────


class TestComputePriceEligibilityRows:
    def test_empty_prices_returns_empty(self):
        assert compute_price_eligibility_rows(
            pd.DataFrame(columns=["ticker", "date", "close", "volume"]),
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        ) == []

    def test_price_usd_emitted_for_every_session_in_range(self):
        dates = _trading_dates(date(2024, 1, 2), 25)
        prices = _make_prices(["AAA"], dates)
        rows = compute_price_eligibility_rows(prices, start=dates[0], end=dates[-1])
        price_rows = [r for r in rows if r["attribute_name"] == "price_usd"]
        assert len(price_rows) == 25
        assert all(r["ticker"] == "AAA" for r in price_rows)

    def test_adv_usd_20d_requires_full_trailing_window(self):
        dates = _trading_dates(date(2024, 1, 2), 25)
        prices = _make_prices(["AAA"], dates)
        rows = compute_price_eligibility_rows(prices, start=dates[0], end=dates[-1])
        adv_rows = [r for r in rows if r["attribute_name"] == "adv_usd_20d"]
        # First 19 sessions have < 20 trailing observations -> no ADV row.
        assert len(adv_rows) == 25 - 19

    def test_adv_value_is_trailing_dollar_volume_mean(self):
        dates = _trading_dates(date(2024, 1, 2), 25)
        prices = _make_prices(["AAA"], dates)
        rows = compute_price_eligibility_rows(prices, start=dates[0], end=dates[-1])
        adv_rows = sorted(
            (r for r in rows if r["attribute_name"] == "adv_usd_20d"),
            key=lambda r: r["effective_start"],
        )
        first_adv_row = adv_rows[0]
        window = prices.iloc[0:20]
        expected = (window["close"] * window["volume"]).mean()
        assert first_adv_row["attribute_value_numeric"] == pytest.approx(expected)

    def test_grain_is_pit_by_construction_not_backward_projected(self):
        """A later day's close must never appear as an earlier day's
        price_usd/adv_usd_20d value (design doc §1.4's core requirement)."""
        dates = _trading_dates(date(2024, 1, 2), 25)
        prices = _make_prices(["AAA"], dates)
        rows = compute_price_eligibility_rows(prices, start=dates[0], end=dates[-1])
        price_rows = {r["effective_start"]: r["attribute_value_numeric"] for r in rows if r["attribute_name"] == "price_usd"}
        for _, row in prices.iterrows():
            assert price_rows[row["date"]] == pytest.approx(float(row["close"]))

    def test_intervals_chain_to_next_session_last_is_open(self):
        dates = _trading_dates(date(2024, 1, 2), 5)
        prices = _make_prices(["AAA"], dates)
        rows = compute_price_eligibility_rows(prices, start=dates[0], end=dates[-1])
        price_rows = sorted(
            (r for r in rows if r["attribute_name"] == "price_usd"),
            key=lambda r: r["effective_start"],
        )
        for i in range(len(price_rows) - 1):
            assert price_rows[i]["effective_end"] == price_rows[i + 1]["effective_start"]
        assert price_rows[-1]["effective_end"] is None

    def test_source_data_asof_never_exceeds_effective_start(self):
        dates = _trading_dates(date(2024, 1, 2), 25)
        prices = _make_prices(["AAA"], dates)
        rows = compute_price_eligibility_rows(prices, start=dates[0], end=dates[-1])
        for r in rows:
            assert r["source_data_asof"] <= r["effective_start"]

    def test_missing_close_skips_price_row_not_fabricated(self):
        dates = _trading_dates(date(2024, 1, 2), 5)
        prices = _make_prices(["AAA"], dates)
        prices.loc[2, "close"] = None
        rows = compute_price_eligibility_rows(prices, start=dates[0], end=dates[-1])
        price_dates = {r["effective_start"] for r in rows if r["attribute_name"] == "price_usd"}
        assert dates[2] not in price_dates


# ─── write_price_eligibility_batch ─────────────────────────────────────────────


class TestWritePriceEligibilityBatch:
    def test_write_persists_batch_and_rows(self, engine):
        dates = _trading_dates(date(2024, 1, 2), 25)
        prices = _make_prices(["AAA", "BBB"], dates)
        result = write_price_eligibility_batch(
            engine, "sp500_test", prices, start=dates[0], end=dates[-1], code_version="test-abc"
        )
        assert result.n_rows_written > 0
        assert result.n_tickers == 2
        assert set(result.attribute_names) == {"adv_usd_20d", "price_usd"}

        with Session(engine) as session:
            batch = session.execute(select(UniverseEligibilityBatch)).scalars().one()
            assert batch.universe_id == "sp500_test"
            assert batch.code_version == "test-abc"
            assert batch.n_attribute_rows == result.n_rows_written

            rows = session.execute(select(UniverseEligibilityAttribute)).scalars().all()
            assert len(rows) == result.n_rows_written
            for r in rows:
                assert r.computation_batch_id == batch.id

    def test_empty_prices_raises_empty_batch_error(self, engine):
        with pytest.raises(EmptyBatchError):
            write_price_eligibility_batch(
                engine,
                "sp500_test",
                pd.DataFrame(columns=["ticker", "date", "close", "volume"]),
                start=date(2024, 1, 2),
                end=date(2024, 1, 31),
                code_version="test-abc",
            )

    def test_repeated_batches_are_independent_append_only_rows(self, engine):
        dates = _trading_dates(date(2024, 1, 2), 25)
        prices = _make_prices(["AAA"], dates)
        r1 = write_price_eligibility_batch(
            engine, "sp500_test", prices, start=dates[0], end=dates[-1], code_version="v1"
        )
        r2 = write_price_eligibility_batch(
            engine, "sp500_test", prices, start=dates[0], end=dates[-1], code_version="v2"
        )
        assert r1.batch_id != r2.batch_id
        with Session(engine) as session:
            batches = session.execute(select(UniverseEligibilityBatch)).scalars().all()
            assert len(batches) == 2
            attrs = session.execute(select(UniverseEligibilityAttribute)).scalars().all()
            # Both batches' rows persist -- nothing was mutated/overwritten.
            assert len(attrs) == r1.n_rows_written + r2.n_rows_written


# ─── security_type curation ─────────────────────────────────────────────────────


class TestBuildSecurityTypeRows:
    def test_default_applied_to_every_uncurated_ticker(self):
        membership = {"AAA": [(date(2020, 1, 1), None)], "BBB": [(date(2020, 1, 1), date(2021, 1, 1))]}
        rows = build_security_type_rows(membership, curation=[])
        assert len(rows) == 2
        assert {r["ticker"]: r["attribute_value_text"] for r in rows} == {"AAA": "CS", "BBB": "CS"}

    def test_curated_ticker_uses_only_curated_entries(self):
        membership = {"AAA": [(date(2020, 1, 1), None)]}
        curation = [
            SecurityTypeCurationEntry(
                ticker="AAA", security_type="REIT", effective_start=date(2020, 1, 1), note="test"
            )
        ]
        rows = build_security_type_rows(membership, curation)
        assert len(rows) == 1
        assert rows[0]["attribute_value_text"] == "REIT"
        assert not rows[0]["computed_from"].startswith("default")

    def test_multiple_non_overlapping_curated_entries_allowed(self):
        membership = {"AAA": [(date(2020, 1, 1), None)]}
        curation = [
            SecurityTypeCurationEntry(
                ticker="AAA", security_type="CS", effective_start=date(2020, 1, 1), effective_end=date(2022, 1, 1)
            ),
            SecurityTypeCurationEntry(
                ticker="AAA", security_type="REIT", effective_start=date(2022, 1, 1)
            ),
        ]
        rows = build_security_type_rows(membership, curation)
        assert len(rows) == 2

    def test_overlapping_curated_entries_rejected(self):
        membership = {"AAA": [(date(2020, 1, 1), None)]}
        curation = [
            SecurityTypeCurationEntry(
                ticker="AAA", security_type="CS", effective_start=date(2020, 1, 1), effective_end=date(2022, 1, 1)
            ),
            SecurityTypeCurationEntry(
                ticker="AAA", security_type="REIT", effective_start=date(2021, 1, 1)
            ),
        ]
        with pytest.raises(SecurityTypeCurationError):
            build_security_type_rows(membership, curation)

    def test_curation_entry_with_invalid_range_rejected(self):
        membership = {"AAA": [(date(2020, 1, 1), None)]}
        curation = [
            SecurityTypeCurationEntry(
                ticker="AAA", security_type="CS", effective_start=date(2022, 1, 1), effective_end=date(2020, 1, 1)
            ),
        ]
        with pytest.raises(SecurityTypeCurationError):
            build_security_type_rows(membership, curation)

    def test_curation_referencing_unknown_ticker_rejected(self):
        membership = {"AAA": [(date(2020, 1, 1), None)]}
        curation = [
            SecurityTypeCurationEntry(
                ticker="ZZZ", security_type="CS", effective_start=date(2020, 1, 1)
            ),
        ]
        with pytest.raises(SecurityTypeCurationError):
            build_security_type_rows(membership, curation)


class TestWriteSecurityTypeBatch:
    def test_write_uses_published_membership(self, engine, published_universe):
        result = write_security_type_batch(engine, published_universe, curation=[], code_version="test-abc")
        assert result.n_rows_written > 0
        assert result.attribute_names == ("security_type",)
        with Session(engine) as session:
            rows = session.execute(select(UniverseEligibilityAttribute)).scalars().all()
            assert all(r.attribute_value_text == "CS" for r in rows)

    def test_no_published_membership_raises_empty_batch_error(self, engine):
        with pytest.raises(EmptyBatchError):
            write_security_type_batch(engine, "never_imported", curation=[], code_version="test-abc")

    def test_curated_ticker_overrides_default(self, engine, published_universe):
        curation = [
            SecurityTypeCurationEntry(
                ticker="AAA", security_type="ADR", effective_start=date(2020, 1, 1), note="test override"
            )
        ]
        result = write_security_type_batch(
            engine, published_universe, curation=curation, code_version="test-abc"
        )
        with Session(engine) as session:
            rows = session.execute(
                select(UniverseEligibilityAttribute).where(UniverseEligibilityAttribute.ticker == "AAA")
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].attribute_value_text == "ADR"


# ─── load_membership_intervals ─────────────────────────────────────────────────


def test_load_membership_intervals_from_published_batch(engine, published_universe):
    intervals = load_membership_intervals(engine, published_universe)
    assert "AAA" in intervals
    assert intervals["AAA"][0][0] == FIXTURE_COVERAGE_START


def test_load_membership_intervals_no_published_batch_returns_empty(engine):
    assert load_membership_intervals(engine, "never_imported") == {}


# ─── eligibility_coverage_report ───────────────────────────────────────────────


class TestEligibilityCoverageReport:
    def test_reports_gap_when_no_attribute_rows_exist(self, engine, published_universe):
        report = eligibility_coverage_report(
            engine, published_universe, [date(2020, 1, 2), date(2020, 1, 3)]
        )
        assert not report.by_date.empty
        assert (report.by_date["n_missing"] == report.by_date["n_members"]).all()

    def test_reports_zero_gap_once_price_attributes_populated(self, engine, published_universe):
        dates = [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6)]
        # Every ticker eligible on these dates in the fixture universe:
        # AAA, DDD (first stint), EEE (pre-rename), GGG, HHH, III, JJJ.
        prices = _make_prices(["AAA", "DDD", "EEE", "GGG", "HHH", "III", "JJJ"], dates)
        write_price_eligibility_batch(
            engine, published_universe, prices, start=dates[0], end=dates[-1], code_version="t1"
        )
        report = eligibility_coverage_report(
            engine, published_universe, dates, attribute_names=("price_usd",)
        )
        assert (report.by_date["n_missing"] == 0).all()

    def test_security_type_curated_vs_default_counts(self, engine, published_universe):
        curation = [
            SecurityTypeCurationEntry(
                ticker="AAA", security_type="ADR", effective_start=date(2020, 1, 1)
            )
        ]
        write_security_type_batch(
            engine, published_universe, curation=curation, code_version="t1"
        )
        report = eligibility_coverage_report(
            engine, published_universe, [date(2020, 1, 2)]
        )
        assert report.n_security_type_curated_tickers == 1
        assert report.n_security_type_default_tickers >= 1

    def test_out_of_scope_market_cap_named_explicitly_not_silently_absent(self, engine, published_universe):
        report = eligibility_coverage_report(engine, published_universe, [date(2020, 1, 2)])
        assert "market_cap_usd" in report.excluded_attributes

    def test_out_of_coverage_date_reports_none_not_a_crash(self, engine, published_universe):
        report = eligibility_coverage_report(
            engine, published_universe, [date(1999, 1, 1)]
        )
        row = report.by_date.iloc[0]
        assert not row["in_coverage"]
        assert row["n_missing"] is None

    def test_no_published_import_reports_all_out_of_coverage(self, engine):
        report = eligibility_coverage_report(engine, "never_imported", [date(2020, 1, 2)])
        assert (report.by_date["in_coverage"] == False).all()  # noqa: E712
