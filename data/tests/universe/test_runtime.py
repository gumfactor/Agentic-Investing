"""Tests for data/universe/runtime.py — the PIT universe runtime API (§1.3).

FIXTURE data only (FixtureSP500Provider). Interval recap:

- AAA: [2020-01-01, open)
- BBB: [2020-06-01, 2021-01-01)
- CCC: [2021-06-01, open)
- DDD: [2020-01-01, 2020-04-01) and [2022-01-01, open)  (re-entry)
- EEE: [2020-01-01, 2021-03-01)  -> renamed to FFF
- FFF: [2021-03-01, open)
Coverage: [2020-01-01, 2024-01-02].
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from data.universe.calendar import session_close_cutoff
from data.universe.import_pipeline import run_import
from data.universe.providers.fixture_provider import (
    FIXTURE_COVERAGE_START,
    FIXTURE_UNIVERSE_ID,
    FixtureSP500Provider,
)
from data.universe.runtime import (
    CoverageGapError,
    CurrentUniverseRejectedError,
    CurrentUniverseSnapshot,
    ExclusionReason,
    HistoricalUniverse,
    InsufficientCrossSectionError,
    NoPublishedImportError,
    PITUniverseLookup,
    load_universe_as_of,
    require_historical_universe,
)


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("runtime_db")
    eng = create_engine(f"sqlite:///{tmp / 'runtime.db'}", future=True)
    run_import(
        FixtureSP500Provider(),
        engine=eng,
        artifact_root=tmp / "artifacts",
        coverage_start=FIXTURE_COVERAGE_START,
    )
    return eng


@pytest.fixture(scope="module")
def lookup(engine) -> PITUniverseLookup:
    return PITUniverseLookup(engine, FIXTURE_UNIVERSE_ID)


class TestFailClosed:
    def test_no_published_import_raises(self, tmp_path: Path) -> None:
        eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
        from data.universe.models import Base

        Base.metadata.create_all(eng)
        with pytest.raises(NoPublishedImportError):
            PITUniverseLookup(eng, "sp500_fixture")

    def test_date_before_coverage_raises(self, lookup) -> None:
        with pytest.raises(CoverageGapError):
            lookup.load_universe_as_of(date(2019, 6, 1))

    def test_date_after_coverage_raises(self, lookup) -> None:
        with pytest.raises(CoverageGapError):
            lookup.load_universe_as_of(date(2025, 1, 1))

    def test_is_eligible_outside_coverage_raises(self, lookup) -> None:
        with pytest.raises(CoverageGapError):
            lookup.is_eligible("AAA", date(2019, 1, 1))

    def test_min_eligible_fails_closed(self, lookup) -> None:
        with pytest.raises(InsufficientCrossSectionError):
            lookup.load_universe_as_of(date(2022, 6, 1), min_eligible=100)


class TestMembershipQueries:
    def test_removed_constituent_included_before_excluded_after(self, lookup) -> None:
        # BBB removed effective 2021-01-01 (half-open: last member day
        # 2020-12-31). Under the date-only source's conservative rule the
        # REMOVAL is knowable only from the next session's close
        # (2021-01-04), so on the removal date itself, with the default
        # same-session cutoff, BBB is still eligible — excluding it earlier
        # would leak future removal information (Codex PR #34 P2 fix).
        assert lookup.is_eligible("BBB", date(2020, 12, 31)) is True
        assert lookup.is_eligible("BBB", date(2021, 1, 1)) is True  # removal not yet knowable
        # Once the removal is knowable, the exclusion applies — both via a
        # later explicit cutoff on the same date and on later dates.
        from datetime import datetime, timezone

        late_cutoff = datetime(2021, 1, 5, tzinfo=timezone.utc)
        assert lookup.is_eligible("BBB", date(2021, 1, 1), observation_cutoff=late_cutoff) is False
        assert lookup.is_eligible("BBB", date(2021, 1, 4)) is False
        assert lookup.is_eligible("BBB", date(2021, 6, 1)) is False

    def test_entrant_excluded_before_included_after(self, lookup) -> None:
        # CCC added effective 2021-06-01. With the conservative date-only
        # known_at rule its membership is knowable only from the next session,
        # so under the default same-session cutoff it becomes eligible one
        # session later.
        assert lookup.is_eligible("CCC", date(2021, 5, 28)) is False
        assert lookup.is_eligible("CCC", date(2021, 6, 3)) is True

    def test_reentry_intervals_both_queryable(self, lookup) -> None:
        assert lookup.is_eligible("DDD", date(2020, 2, 1)) is True
        assert lookup.is_eligible("DDD", date(2021, 1, 1)) is False  # between stints
        assert lookup.is_eligible("DDD", date(2022, 6, 1)) is True

    def test_per_ticker_absence_is_valid_non_membership(self, lookup) -> None:
        # Never-member ticker inside coverage: False, not an exception.
        assert lookup.is_eligible("NOPE", date(2022, 6, 1)) is False

    def test_renamed_ticker_old_symbol_eligible_only_before_rename(self, lookup) -> None:
        assert lookup.is_eligible("EEE", date(2021, 2, 26)) is True
        assert lookup.is_eligible("EEE", date(2021, 3, 2)) is False
        assert lookup.is_eligible("FFF", date(2021, 3, 3)) is True


class TestKnowledgeCutoff:
    def test_effective_start_session_not_knowable_same_session(self, lookup) -> None:
        # Date-only source: membership starting 2021-06-01 has known_at at
        # the close of the NEXT session, so it is NOT eligible under the
        # same-session close cutoff — the "after-close announcement applied
        # on the same session" failure mode is structurally impossible.
        assert lookup.is_eligible("CCC", date(2021, 6, 1)) is False
        result = lookup.load_universe_as_of(date(2021, 6, 1))
        assert "CCC" not in result.eligible_tickers
        excl = {e.ticker: e for e in result.exclusions}
        assert "CCC" in excl
        assert excl["CCC"].reason == ExclusionReason.NOT_KNOWN_BY_CUTOFF

    def test_later_cutoff_admits_membership(self, lookup) -> None:
        late_cutoff = datetime(2021, 6, 10, tzinfo=timezone.utc)
        assert lookup.is_eligible("CCC", date(2021, 6, 1), observation_cutoff=late_cutoff) is True

    def test_default_cutoff_is_session_close(self, lookup) -> None:
        result = lookup.load_universe_as_of(date(2022, 6, 1))
        assert result.observation_cutoff == session_close_cutoff(date(2022, 6, 1))


class TestLoadUniverseAsOf:
    def test_returns_historical_universe_with_provenance(self, engine) -> None:
        result = load_universe_as_of(
            FIXTURE_UNIVERSE_ID, date(2022, 6, 1), engine=engine
        )
        assert isinstance(result, HistoricalUniverse)
        assert sorted(result.eligible_tickers) == [
            "AAA", "CCC", "DDD", "FFF", "GGG", "HHH", "III", "JJJ",
        ]
        assert result.source == "fixture_sp500"
        assert result.import_batch_id >= 1
        assert result.coverage_start == FIXTURE_COVERAGE_START

    def test_contains_protocol(self, engine) -> None:
        result = load_universe_as_of(FIXTURE_UNIVERSE_ID, date(2022, 6, 1), engine=engine)
        assert "AAA" in result
        assert "BBB" not in result


class TestTypeLevelEnforcement:
    def test_current_universe_snapshot_rejected(self) -> None:
        snap = CurrentUniverseSnapshot(
            operational_tickers=("AAPL", "MSFT"),
            fetched_at=datetime.now(tz=timezone.utc),
            source="test",
        )
        with pytest.raises(CurrentUniverseRejectedError):
            require_historical_universe(snap)

    def test_plain_list_rejected(self) -> None:
        with pytest.raises(CurrentUniverseRejectedError):
            require_historical_universe(["AAPL", "MSFT"])

    def test_historical_universe_accepted(self, engine) -> None:
        result = load_universe_as_of(FIXTURE_UNIVERSE_ID, date(2022, 6, 1), engine=engine)
        assert require_historical_universe(result) is result

    def test_current_snapshot_does_not_expose_historical_protocol(self) -> None:
        snap = CurrentUniverseSnapshot(
            operational_tickers=("AAPL",),
            fetched_at=datetime.now(tz=timezone.utc),
            source="test",
        )
        assert not hasattr(snap, "eligible_tickers")
        assert not hasattr(snap, "as_of_date")
