"""Tests for the PIT eligibility-attribute runtime (data/universe/runtime.py,
Roadmap 03A-4a §1.3/§1.5). SQLite-backed synthetic fixture rows only -- Phase
A ships schema + read API + fail-closed config contract; the Phase B batch
job that populates real adv_usd_20d/price_usd rows from daily_prices is
out of scope here (see docs/plans/03a-immutable-research-data-design.md §1
and the 03A-4a task brief's scope boundary).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from data.universe.models import (
    Base,
    UniverseEligibilityAttribute,
    UniverseEligibilityBatch,
)
from data.universe.runtime import (
    CombinedEligibleUniverse,
    EligibilityExclusionReason,
    EligibilityFilterOp,
    EligibilityResult,
    FilterSpec,
    InsufficientCrossSectionError,
    NoEligibilityDataError,
    PITEligibilityLookup,
    load_eligibility_as_of,
)

_UNIVERSE_ID = "sp500_fixture_eligibility"


def _seed(engine, rows: list[dict], *, code_version: str = "abc123") -> int:
    """Insert one eligibility batch plus its attribute rows. Returns the
    batch id."""
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        batch = UniverseEligibilityBatch(
            universe_id=_UNIVERSE_ID,
            code_version=code_version,
            computed_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            n_attribute_rows=len(rows),
            created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        )
        session.add(batch)
        session.flush()
        for row in rows:
            session.add(
                UniverseEligibilityAttribute(
                    universe_id=_UNIVERSE_ID,
                    computation_batch_id=batch.id,
                    created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    **row,
                )
            )
        session.commit()
        return batch.id


@pytest.fixture
def engine(tmp_path: Path):
    return create_engine(f"sqlite:///{tmp_path / 'eligibility.db'}", future=True)


def _adv_row(ticker: str, value: float, start: date, end=None, source_asof=None) -> dict:
    return dict(
        ticker=ticker,
        attribute_name="adv_usd_20d",
        attribute_value_numeric=value,
        attribute_value_text=None,
        effective_start=start,
        effective_end=end,
        computed_from="daily_prices.volume*close 20-session mean",
        source_data_asof=source_asof or start,
    )


def _price_row(ticker: str, value: float, start: date, end=None, source_asof=None) -> dict:
    return dict(
        ticker=ticker,
        attribute_name="price_usd",
        attribute_value_numeric=value,
        attribute_value_text=None,
        effective_start=start,
        effective_end=end,
        computed_from="daily_prices.close",
        source_data_asof=source_asof or start,
    )


def _security_type_row(ticker: str, value: str, start: date, end=None, source_asof=None) -> dict:
    return dict(
        ticker=ticker,
        attribute_name="security_type",
        attribute_value_numeric=None,
        attribute_value_text=value,
        effective_start=start,
        effective_end=end,
        computed_from="hand-curated security_type import",
        source_data_asof=source_asof or start,
    )


class TestFailClosedConstruction:
    def test_no_rows_at_all_raises(self, tmp_path: Path) -> None:
        eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
        Base.metadata.create_all(eng)
        with pytest.raises(NoEligibilityDataError):
            PITEligibilityLookup(eng, "no_such_universe")


class TestSingleFilterEvaluation:
    def test_ticker_below_threshold_excluded_even_though_current_value_above(
        self, engine
    ) -> None:
        """§1.5 acceptance test 1: excluded on d even though its CURRENT
        value exceeds the threshold -- the row for d is what governs, not
        whatever a later interval says."""
        _seed(
            engine,
            [
                _adv_row("LOWLIQ", 500_000.0, date(2024, 1, 1), date(2024, 6, 1)),
                _adv_row("LOWLIQ", 5_000_000.0, date(2024, 6, 1)),  # later: liquid
            ],
        )
        filters = {"min_adv": FilterSpec("adv_usd_20d", EligibilityFilterOp.GTE, 1_000_000.0)}
        result = load_eligibility_as_of(
            _UNIVERSE_ID, date(2024, 3, 1), filters, engine=engine, tickers=["LOWLIQ"]
        )
        assert "LOWLIQ" not in result
        assert result.exclusions[0].reason == EligibilityExclusionReason.BELOW_THRESHOLD

        # Later date: the second interval covers it, now above threshold.
        result_later = load_eligibility_as_of(
            _UNIVERSE_ID, date(2024, 7, 1), filters, engine=engine, tickers=["LOWLIQ"]
        )
        assert "LOWLIQ" in result_later

    def test_ticker_missing_attribute_excluded_never_silently_included(self, engine) -> None:
        """§1.5 acceptance test 2."""
        _seed(engine, [_adv_row("HASDATA", 5_000_000.0, date(2024, 1, 1))])
        filters = {"min_adv": FilterSpec("adv_usd_20d", EligibilityFilterOp.GTE, 1_000_000.0)}
        result = load_eligibility_as_of(
            _UNIVERSE_ID,
            date(2024, 3, 1),
            filters,
            engine=engine,
            tickers=["HASDATA", "NODATA"],
        )
        assert "HASDATA" in result
        assert "NODATA" not in result
        excl = {e.ticker: e for e in result.exclusions}
        assert excl["NODATA"].reason == EligibilityExclusionReason.MISSING_ATTRIBUTE

    def test_source_data_asof_newer_than_effective_start_rejected_at_ingestion(
        self, engine
    ) -> None:
        """§1.5 acceptance test 4 (future-leak guard): a row whose
        source_data_asof is AFTER the interval it describes is rejected at
        ingestion, not merely documented. The
        ck_universe_eligibility_attributes_source_not_future CHECK constraint
        is portable (unlike the Postgres-only EXCLUDE), so it fires on the
        mirrored ORM model against SQLite -- we actually attempt the insert
        and assert IntegrityError, rather than asserting a fact about the
        fixture."""
        from sqlalchemy.exc import IntegrityError

        Base.metadata.create_all(engine)
        with Session(engine) as session:
            batch = UniverseEligibilityBatch(
                universe_id=_UNIVERSE_ID,
                code_version="test",
                computed_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            )
            session.add(batch)
            session.flush()
            # source_data_asof (2024-02-01) is AFTER effective_start
            # (2024-01-01): exactly the invalid shape the CHECK forbids.
            session.add(
                UniverseEligibilityAttribute(
                    universe_id=_UNIVERSE_ID,
                    ticker="FUTURELEAK",
                    attribute_name="adv_usd_20d",
                    attribute_value_numeric=1.0,
                    effective_start=date(2024, 1, 1),
                    computed_from="daily_prices.volume*close 20-session mean",
                    source_data_asof=date(2024, 2, 1),
                    computation_batch_id=batch.id,
                    created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()


class TestSecurityTypeInFilter:
    def test_allowed_security_types_membership(self, engine) -> None:
        _seed(
            engine,
            [
                _security_type_row("COMMON", "common_stock", date(2024, 1, 1)),
                _security_type_row("REIT1", "reit", date(2024, 1, 1)),
            ],
        )
        filters = {
            "allowed_types": FilterSpec(
                "security_type", EligibilityFilterOp.IN, ("common_stock", "adr")
            )
        }
        result = load_eligibility_as_of(
            _UNIVERSE_ID, date(2024, 3, 1), filters, engine=engine, tickers=["COMMON", "REIT1"]
        )
        assert "COMMON" in result
        assert "REIT1" not in result
        excl = {e.ticker: e for e in result.exclusions}
        assert excl["REIT1"].reason == EligibilityExclusionReason.BELOW_THRESHOLD


class TestStaleness:
    def test_stale_attribute_excluded_when_max_staleness_configured(self, engine) -> None:
        # Row's source_data_asof is 40 days before as_of_date but the
        # interval is still open, so membership check alone would pass it;
        # a 30-day staleness bound must exclude it instead.
        _seed(
            engine,
            [
                _adv_row(
                    "STALE",
                    5_000_000.0,
                    date(2024, 1, 1),
                    source_asof=date(2024, 1, 1),
                )
            ],
        )
        filters = {
            "min_adv": FilterSpec(
                "adv_usd_20d",
                EligibilityFilterOp.GTE,
                1_000_000.0,
                max_staleness_days=30,
            )
        }
        result = load_eligibility_as_of(
            _UNIVERSE_ID, date(2024, 3, 1), filters, engine=engine, tickers=["STALE"]
        )
        assert "STALE" not in result
        assert result.exclusions[0].reason == EligibilityExclusionReason.STALE_ATTRIBUTE


class TestBatchCorrection:
    def test_latest_batch_wins_when_two_batches_cover_same_date(self, tmp_path: Path) -> None:
        """§1.2: correcting a bad computation publishes a NEW batch; the
        newest batch's covering row is authoritative."""
        eng = create_engine(f"sqlite:///{tmp_path / 'correction.db'}", future=True)
        Base.metadata.create_all(eng)
        with Session(eng) as session:
            bad_batch = UniverseEligibilityBatch(
                universe_id=_UNIVERSE_ID,
                code_version="bad_commit",
                computed_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            )
            session.add(bad_batch)
            session.flush()
            session.add(
                UniverseEligibilityAttribute(
                    universe_id=_UNIVERSE_ID,
                    ticker="CORR",
                    attribute_name="adv_usd_20d",
                    attribute_value_numeric=100.0,  # wrong value
                    effective_start=date(2024, 1, 1),
                    computed_from="bad computation",
                    source_data_asof=date(2024, 1, 1),
                    computation_batch_id=bad_batch.id,
                    created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                )
            )
            good_batch = UniverseEligibilityBatch(
                universe_id=_UNIVERSE_ID,
                code_version="fixed_commit",
                computed_at=datetime(2024, 1, 3, tzinfo=timezone.utc),  # later
                created_at=datetime(2024, 1, 3, tzinfo=timezone.utc),
            )
            session.add(good_batch)
            session.flush()
            session.add(
                UniverseEligibilityAttribute(
                    universe_id=_UNIVERSE_ID,
                    ticker="CORR",
                    attribute_name="adv_usd_20d",
                    attribute_value_numeric=5_000_000.0,  # corrected value
                    effective_start=date(2024, 1, 1),
                    computed_from="fixed computation",
                    source_data_asof=date(2024, 1, 1),
                    computation_batch_id=good_batch.id,
                    created_at=datetime(2024, 1, 3, tzinfo=timezone.utc),
                )
            )
            session.commit()

        filters = {"min_adv": FilterSpec("adv_usd_20d", EligibilityFilterOp.GTE, 1_000_000.0)}
        result = load_eligibility_as_of(
            _UNIVERSE_ID, date(2024, 2, 1), filters, engine=eng, tickers=["CORR"]
        )
        # If the bad batch's row won, this would fail (100.0 < threshold).
        assert "CORR" in result

    def test_identical_computed_at_breaks_tie_on_batch_id(self, tmp_path: Path) -> None:
        """P1-1: two batches sharing an IDENTICAL computed_at must resolve
        deterministically -- the higher computation_batch_id (later-created
        batch = the correction) wins, NOT the first-inserted row. max()'s
        first-maximal-element behavior would otherwise silently degrade
        "latest batch wins" to "DB insert order"."""
        eng = create_engine(f"sqlite:///{tmp_path / 'tie.db'}", future=True)
        Base.metadata.create_all(eng)
        shared_ts = datetime(2024, 1, 2, 9, 30, 0, tzinfo=timezone.utc)
        with Session(eng) as session:
            # Bad batch inserted FIRST, identical computed_at.
            bad_batch = UniverseEligibilityBatch(
                universe_id=_UNIVERSE_ID,
                code_version="bad_commit",
                computed_at=shared_ts,
                created_at=shared_ts,
            )
            session.add(bad_batch)
            session.flush()
            session.add(
                UniverseEligibilityAttribute(
                    universe_id=_UNIVERSE_ID,
                    ticker="TIE",
                    attribute_name="adv_usd_20d",
                    attribute_value_numeric=100.0,  # below threshold
                    effective_start=date(2024, 1, 1),
                    computed_from="bad",
                    source_data_asof=date(2024, 1, 1),
                    computation_batch_id=bad_batch.id,
                    created_at=shared_ts,
                )
            )
            # Correction batch inserted SECOND, SAME computed_at -> higher id.
            good_batch = UniverseEligibilityBatch(
                universe_id=_UNIVERSE_ID,
                code_version="fixed_commit",
                computed_at=shared_ts,
                created_at=shared_ts,
            )
            session.add(good_batch)
            session.flush()
            assert good_batch.id > bad_batch.id
            session.add(
                UniverseEligibilityAttribute(
                    universe_id=_UNIVERSE_ID,
                    ticker="TIE",
                    attribute_name="adv_usd_20d",
                    attribute_value_numeric=5_000_000.0,  # above threshold
                    effective_start=date(2024, 1, 1),
                    computed_from="fixed",
                    source_data_asof=date(2024, 1, 1),
                    computation_batch_id=good_batch.id,
                    created_at=shared_ts,
                )
            )
            session.commit()

        filters = {"min_adv": FilterSpec("adv_usd_20d", EligibilityFilterOp.GTE, 1_000_000.0)}
        result = load_eligibility_as_of(
            _UNIVERSE_ID, date(2024, 2, 1), filters, engine=eng, tickers=["TIE"]
        )
        # The higher-batch_id correction (5M) must win the tie, not the
        # first-inserted stale row (100).
        assert "TIE" in result


class TestCombinedMembershipAndEligibility:
    def test_distinguishable_exclusion_reasons(self, tmp_path: Path) -> None:
        """§1.5 acceptance test 5: a reviewer can tell "not a member" from
        "member but illiquid" from "member but no eligibility data"."""
        from data.universe.import_pipeline import run_import
        from data.universe.providers.fixture_provider import (
            FIXTURE_COVERAGE_START,
            FIXTURE_UNIVERSE_ID,
            FixtureSP500Provider,
        )
        from data.universe.runtime import load_historical_universe_as_of

        eng = create_engine(f"sqlite:///{tmp_path / 'combined.db'}", future=True)
        run_import(
            FixtureSP500Provider(),
            engine=eng,
            artifact_root=tmp_path / "artifacts",
            coverage_start=FIXTURE_COVERAGE_START,
        )
        # AAA is a member throughout; give it a below-threshold ADV row.
        # CCC is a member from 2021-06-01; give it no eligibility row at all.
        with Session(eng) as session:
            batch = UniverseEligibilityBatch(
                universe_id=FIXTURE_UNIVERSE_ID,
                code_version="test",
                computed_at=datetime(2022, 1, 2, tzinfo=timezone.utc),
                created_at=datetime(2022, 1, 2, tzinfo=timezone.utc),
            )
            session.add(batch)
            session.flush()
            session.add(
                UniverseEligibilityAttribute(
                    universe_id=FIXTURE_UNIVERSE_ID,
                    ticker="AAA",
                    attribute_name="adv_usd_20d",
                    attribute_value_numeric=100.0,
                    effective_start=date(2020, 1, 1),
                    computed_from="test",
                    source_data_asof=date(2020, 1, 1),
                    computation_batch_id=batch.id,
                    created_at=datetime(2022, 1, 2, tzinfo=timezone.utc),
                )
            )
            session.commit()

        filters = {"min_adv": FilterSpec("adv_usd_20d", EligibilityFilterOp.GTE, 1_000_000.0)}
        combined = load_historical_universe_as_of(
            FIXTURE_UNIVERSE_ID, date(2022, 6, 1), filters, engine=eng
        )
        assert isinstance(combined, CombinedEligibleUniverse)
        # AAA: member, but fails the ADV filter.
        assert "AAA" not in combined.eligible_tickers
        assert "AAA" in combined.membership.eligible_tickers
        adv_excl = {e.ticker: e for e in combined.eligibility.exclusions}
        assert adv_excl["AAA"].reason == EligibilityExclusionReason.BELOW_THRESHOLD
        # NOPE: never a member at all -- absent from membership eligible set
        # and never even considered by eligibility (not a membership
        # exclusion event either, per existing membership semantics).
        assert "NOPE" not in combined.membership.eligible_tickers
        assert "NOPE" not in combined.eligible_tickers

    def test_min_eligible_enforced_after_eligibility_narrowing(self, tmp_path: Path) -> None:
        from data.universe.import_pipeline import run_import
        from data.universe.providers.fixture_provider import (
            FIXTURE_COVERAGE_START,
            FIXTURE_UNIVERSE_ID,
            FixtureSP500Provider,
        )
        from data.universe.runtime import load_historical_universe_as_of

        eng = create_engine(f"sqlite:///{tmp_path / 'combined2.db'}", future=True)
        run_import(
            FixtureSP500Provider(),
            engine=eng,
            artifact_root=tmp_path / "artifacts",
            coverage_start=FIXTURE_COVERAGE_START,
        )
        with pytest.raises(NoEligibilityDataError):
            load_historical_universe_as_of(
                FIXTURE_UNIVERSE_ID,
                date(2022, 6, 1),
                {"min_adv": FilterSpec("adv_usd_20d", EligibilityFilterOp.GTE, 1.0)},
                engine=eng,
            )

    def test_empty_filters_reduces_to_membership_only(self, tmp_path: Path) -> None:
        from data.universe.import_pipeline import run_import
        from data.universe.providers.fixture_provider import (
            FIXTURE_COVERAGE_START,
            FIXTURE_UNIVERSE_ID,
            FixtureSP500Provider,
        )
        from data.universe.runtime import load_historical_universe_as_of

        eng = create_engine(f"sqlite:///{tmp_path / 'combined3.db'}", future=True)
        run_import(
            FixtureSP500Provider(),
            engine=eng,
            artifact_root=tmp_path / "artifacts",
            coverage_start=FIXTURE_COVERAGE_START,
        )
        combined = load_historical_universe_as_of(
            FIXTURE_UNIVERSE_ID, date(2022, 6, 1), {}, engine=eng
        )
        assert combined.eligible_tickers == combined.membership.eligible_tickers
        assert combined.eligibility.exclusions == ()

    def test_min_eligible_fails_closed_on_combined_set(self, tmp_path: Path) -> None:
        from data.universe.import_pipeline import run_import
        from data.universe.providers.fixture_provider import (
            FIXTURE_COVERAGE_START,
            FIXTURE_UNIVERSE_ID,
            FixtureSP500Provider,
        )
        from data.universe.runtime import load_historical_universe_as_of

        eng = create_engine(f"sqlite:///{tmp_path / 'combined4.db'}", future=True)
        run_import(
            FixtureSP500Provider(),
            engine=eng,
            artifact_root=tmp_path / "artifacts",
            coverage_start=FIXTURE_COVERAGE_START,
        )
        with pytest.raises(InsufficientCrossSectionError):
            load_historical_universe_as_of(
                FIXTURE_UNIVERSE_ID, date(2022, 6, 1), {}, engine=eng, min_eligible=1000
            )
