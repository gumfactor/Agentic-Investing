"""Tests for data/universe/import_pipeline.py using FIXTURE data only.

No network access. The FixtureSP500Provider (data/universe/providers/
fixture_provider.py) is deterministic synthetic data clearly labeled as a
fixture (universe_id="sp500_fixture") — never confused with a real import.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from data.universe.import_pipeline import (
    ImportValidationError,
    apply_exclusions,
    MembershipCandidate,
    StagingBundle,
    SymbolHistoryCandidate,
    build_staging_records,
    coverage_report,
    derive_known_at,
    persist_raw_snapshot,
    publish,
    record_rejected_batch,
    run_import,
    validate_staging,
)
from data.universe.models import SymbolHistory, UniverseImportBatch, UniverseMembership
from data.universe.providers.base import ChangeEvent, CurrentConstituentRow, ParsedConstituentData
from data.universe.providers.fixture_provider import (
    FIXTURE_COVERAGE_START,
    FIXTURE_UNIVERSE_ID,
    FixtureSP500Provider,
)


@pytest.fixture
def engine(tmp_path: Path):
    return create_engine(f"sqlite:///{tmp_path / 'import_test.db'}", future=True)


@pytest.fixture
def parsed_fixture() -> ParsedConstituentData:
    provider = FixtureSP500Provider()
    raw = provider.fetch()
    return provider.parse(raw)


# ─── Step 1: raw persistence ──────────────────────────────────────────────────


class TestPersistRawSnapshot:
    def test_writes_raw_file_and_manifest_with_checksum(self, tmp_path: Path) -> None:
        provider = FixtureSP500Provider()
        raw = provider.fetch()
        raw_path, checksum = persist_raw_snapshot(raw, tmp_path)

        assert raw_path.exists()
        manifest_path = raw_path.parent / "manifest.json"
        assert manifest_path.exists()

        import hashlib
        assert checksum == hashlib.sha256(raw.content).hexdigest()

        import json
        manifest = json.loads(manifest_path.read_text())
        assert manifest["checksum_sha256"] == checksum
        assert manifest["provider_name"] == "fixture_sp500"


# ─── Step 2: staging normalization ────────────────────────────────────────────


class TestBuildStagingRecords:
    def test_always_active_member_has_open_interval(self, parsed_fixture) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        aaa_rows = [r for r in bundle.membership if r.ticker == "AAA"]
        assert len(aaa_rows) == 1
        assert aaa_rows[0].effective_start == date(2020, 1, 1)
        assert aaa_rows[0].effective_end is None

    def test_removed_constituent_has_closed_interval(self, parsed_fixture) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        bbb_rows = [r for r in bundle.membership if r.ticker == "BBB"]
        assert len(bbb_rows) == 1
        assert bbb_rows[0].effective_start == date(2020, 6, 1)
        assert bbb_rows[0].effective_end == date(2021, 1, 1)

    def test_entrant_has_open_interval_starting_after_coverage_start(self, parsed_fixture) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        ccc_rows = [r for r in bundle.membership if r.ticker == "CCC"]
        assert len(ccc_rows) == 1
        assert ccc_rows[0].effective_start == date(2021, 6, 1)
        assert ccc_rows[0].effective_end is None

    def test_remove_then_reenter_produces_two_disjoint_intervals(self, parsed_fixture) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        ddd_rows = sorted(
            (r for r in bundle.membership if r.ticker == "DDD"), key=lambda r: r.effective_start
        )
        assert len(ddd_rows) == 2
        assert ddd_rows[0].effective_start == date(2020, 1, 1)
        assert ddd_rows[0].effective_end == date(2020, 4, 1)
        assert ddd_rows[1].effective_start == date(2022, 1, 1)
        assert ddd_rows[1].effective_end is None

    def test_duplicate_current_vs_changes_addition_does_not_create_extra_interval(
        self, parsed_fixture
    ) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        aaa_rows = [r for r in bundle.membership if r.ticker == "AAA"]
        ccc_rows = [r for r in bundle.membership if r.ticker == "CCC"]
        assert len(aaa_rows) == 1
        assert len(ccc_rows) == 1

    def test_ticker_rename_produces_symbol_history_row_not_membership_gap(
        self, parsed_fixture
    ) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        renames = [sh for sh in bundle.symbol_history if sh.old_ticker == "EEE"]
        assert len(renames) == 1
        assert renames[0].new_ticker == "FFF"
        assert renames[0].effective_date == date(2021, 3, 1)

        eee_rows = [r for r in bundle.membership if r.ticker == "EEE"]
        fff_rows = [r for r in bundle.membership if r.ticker == "FFF"]
        assert len(eee_rows) == 1
        assert eee_rows[0].effective_start == date(2020, 1, 1)
        assert eee_rows[0].effective_end == date(2021, 3, 1)
        assert len(fff_rows) == 1
        assert fff_rows[0].effective_start == date(2021, 3, 1)
        assert fff_rows[0].effective_end is None

    def test_left_censored_removal_with_no_prior_added_event(self) -> None:
        parsed = ParsedConstituentData(
            universe_id="sp500_fixture",
            current_rows=[],
            change_events=[
                ChangeEvent(
                    effective_date=date(2020, 3, 1),
                    added_ticker=None,
                    added_security_name=None,
                    removed_ticker="ZZZ",
                    removed_security_name="Zulu Fixture Co",
                    reason="Market capitalization change.",
                    source_record_id="chg-zzz",
                )
            ],
        )
        bundle = build_staging_records(
            parsed, coverage_start=date(2019, 1, 1), source="fixture_sp500", source_version="v1"
        )
        zzz_rows = [r for r in bundle.membership if r.ticker == "ZZZ"]
        assert len(zzz_rows) == 1
        assert zzz_rows[0].effective_start == date(2019, 1, 1)
        assert zzz_rows[0].effective_end == date(2020, 3, 1)
        assert zzz_rows[0].left_censored is True


# ─── Step 3: validation ───────────────────────────────────────────────────────


class TestValidateStaging:
    def test_fixture_bundle_is_valid(self, parsed_fixture) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        issues = validate_staging(bundle, coverage_end=date(2024, 1, 1))
        assert issues == []

    def test_overlapping_intervals_rejected(self) -> None:
        bundle = StagingBundle(universe_id="sp500_fixture", coverage_start=date(2019, 1, 1))
        bundle.membership = [
            MembershipCandidate(
                ticker="AAA", vendor_symbol=None, effective_start=date(2020, 1, 1),
                effective_end=date(2021, 1, 1), source="fixture_sp500", source_record_id="a",
                reason=None,
            ),
            MembershipCandidate(
                ticker="AAA", vendor_symbol=None, effective_start=date(2020, 6, 1),
                effective_end=None, source="fixture_sp500", source_record_id="b",
                reason=None,
            ),
        ]
        issues = validate_staging(bundle, coverage_end=date(2022, 1, 1))
        assert any("overlapping_intervals" in i for i in issues)

    def test_adjacent_intervals_allowed(self) -> None:
        bundle = StagingBundle(universe_id="sp500_fixture", coverage_start=date(2019, 1, 1))
        bundle.membership = [
            MembershipCandidate(
                ticker="AAA", vendor_symbol=None, effective_start=date(2019, 1, 1),
                effective_end=date(2020, 1, 1), source="fixture_sp500", source_record_id="a",
                reason=None,
            ),
            MembershipCandidate(
                ticker="AAA", vendor_symbol=None, effective_start=date(2020, 1, 1),
                effective_end=None, source="fixture_sp500", source_record_id="b",
                reason=None,
            ),
        ]
        issues = validate_staging(bundle, coverage_end=date(2022, 1, 1))
        assert not any("overlapping_intervals" in i for i in issues)

    def test_inverted_dates_rejected(self) -> None:
        bundle = StagingBundle(universe_id="sp500_fixture", coverage_start=date(2019, 1, 1))
        bundle.membership = [
            MembershipCandidate(
                ticker="AAA", vendor_symbol=None, effective_start=date(2021, 1, 1),
                effective_end=date(2020, 1, 1), source="fixture_sp500", source_record_id="a",
                reason=None,
            ),
        ]
        issues = validate_staging(bundle, coverage_end=date(2022, 1, 1))
        assert any("inverted_or_empty_range" in i for i in issues)

    def test_unknown_symbol_rejected(self) -> None:
        bundle = StagingBundle(universe_id="sp500_fixture", coverage_start=date(2019, 1, 1))
        bundle.membership = [
            MembershipCandidate(
                ticker="", vendor_symbol=None, effective_start=date(2020, 1, 1),
                effective_end=None, source="fixture_sp500", source_record_id="a",
                reason=None,
            ),
        ]
        issues = validate_staging(bundle, coverage_end=date(2022, 1, 1))
        assert any("unknown_symbol" in i for i in issues)

    def test_global_coverage_gap_rejected(self) -> None:
        # Only member's interval ends well before coverage_end -> a global gap follows.
        bundle = StagingBundle(universe_id="sp500_fixture", coverage_start=date(2019, 1, 1))
        bundle.membership = [
            MembershipCandidate(
                ticker="AAA", vendor_symbol=None, effective_start=date(2019, 1, 1),
                effective_end=date(2020, 1, 1), source="fixture_sp500", source_record_id="a",
                reason=None,
            ),
        ]
        issues = validate_staging(bundle, coverage_end=date(2021, 1, 1))
        assert any("global_coverage_gap" in i for i in issues)

    def test_per_ticker_absence_is_not_a_coverage_failure(self, parsed_fixture) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        issues = validate_staging(bundle, coverage_end=date(2024, 1, 1))
        # "ZZZZ" was never a member anywhere in the fixture; that alone must
        # not produce any issue (checked implicitly: the always-valid fixture
        # bundle above has no member named ZZZZ and still validates clean).
        assert not any("ZZZZ" in i for i in issues)

    def test_future_announced_rejected(self) -> None:
        bundle = StagingBundle(universe_id="sp500_fixture", coverage_start=date(2019, 1, 1))
        bundle.membership = [
            MembershipCandidate(
                ticker="AAA", vendor_symbol=None, effective_start=date(2020, 1, 1),
                effective_end=None, source="fixture_sp500", source_record_id="a",
                reason=None, announced_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            ),
        ]
        issues = validate_staging(
            bundle, coverage_end=date(2022, 1, 1), ingested_at=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )
        assert any("future_announced" in i for i in issues)


# ─── Step 4: known_at derivation ───────────────────────────────────────────────


class TestDeriveKnownAt:
    def test_after_close_same_session_is_excluded_by_construction(self, parsed_fixture) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        bundle = derive_known_at(bundle)
        ccc_row = next(r for r in bundle.membership if r.ticker == "CCC")
        # known_at must fall strictly after CCC's own effective_start session
        # close — a date-only source can never qualify its own session.
        from data.universe.calendar import session_close_cutoff

        assert ccc_row.known_at > session_close_cutoff(ccc_row.effective_start)

    def test_symbol_history_known_at_derived(self, parsed_fixture) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        bundle = derive_known_at(bundle)
        assert all(sh.known_at is not None for sh in bundle.symbol_history)


# ─── Step 5 + 6 + orchestration ────────────────────────────────────────────────


class TestPublishAndRunImport:
    def test_run_import_publishes_fixture(self, engine, tmp_path: Path) -> None:
        provider = FixtureSP500Provider()
        batch = run_import(
            provider, engine=engine, artifact_root=tmp_path, coverage_start=FIXTURE_COVERAGE_START
        )
        assert batch.status == "published"
        assert batch.universe_id == FIXTURE_UNIVERSE_ID
        assert batch.n_membership_rows and batch.n_membership_rows > 0

        with Session(engine) as session:
            n_rows = len(session.execute(select(UniverseMembership)).scalars().all())
            n_sh = len(session.execute(select(SymbolHistory)).scalars().all())
        assert n_rows == batch.n_membership_rows
        assert n_sh == batch.n_symbol_history_rows

    def test_run_import_rejects_and_records_invalid_bundle(self, engine, tmp_path: Path, monkeypatch) -> None:
        provider = FixtureSP500Provider()

        def _bad_build(*args, **kwargs):
            bundle = StagingBundle(universe_id="sp500_fixture", coverage_start=FIXTURE_COVERAGE_START)
            bundle.membership = [
                MembershipCandidate(
                    ticker="AAA", vendor_symbol=None, effective_start=date(2021, 1, 1),
                    effective_end=date(2020, 1, 1), source="fixture_sp500", source_record_id="bad",
                    reason=None,
                )
            ]
            return bundle

        monkeypatch.setattr("data.universe.import_pipeline.build_staging_records", _bad_build)

        with pytest.raises(ImportValidationError):
            run_import(provider, engine=engine, artifact_root=tmp_path, coverage_start=FIXTURE_COVERAGE_START)

        with Session(engine) as session:
            batches = session.execute(select(UniverseImportBatch)).scalars().all()
        assert len(batches) == 1
        assert batches[0].status == "rejected"
        # No membership rows written for a rejected import.
        with Session(engine) as session:
            n_rows = len(session.execute(select(UniverseMembership)).scalars().all())
        assert n_rows == 0

    def test_publish_rejects_bundle_missing_known_at(self, engine, tmp_path: Path, parsed_fixture) -> None:
        bundle = build_staging_records(
            parsed_fixture, coverage_start=FIXTURE_COVERAGE_START, source="fixture_sp500", source_version="v1"
        )
        # Deliberately skip derive_known_at().
        with pytest.raises(ImportValidationError, match="known_at_not_derived"):
            publish(
                bundle,
                engine=engine,
                provider_name="fixture_sp500",
                source_version="v1",
                raw_artifact_path="x",
                raw_checksum_sha256="a" * 64,
                retrieved_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                coverage_start=FIXTURE_COVERAGE_START,
                coverage_end=date(2024, 1, 1),
            )

    def test_coverage_report_counts_members_by_date(self, engine, tmp_path: Path) -> None:
        provider = FixtureSP500Provider()
        run_import(provider, engine=engine, artifact_root=tmp_path, coverage_start=FIXTURE_COVERAGE_START)

        report = coverage_report(
            engine, FIXTURE_UNIVERSE_ID, dates=[date(2020, 3, 1), date(2020, 7, 1), date(2022, 6, 1)]
        )
        by_date = report.by_date.set_index("date")
        # 2020-03-01: AAA, DDD (first stint), EEE + GGG/HHH/III/JJJ active.
        assert by_date.loc[date(2020, 3, 1), "n_members"] == 7
        # 2020-07-01: AAA, BBB, EEE + GGG/HHH/III/JJJ (DDD stint 1 closed 04-01).
        assert by_date.loc[date(2020, 7, 1), "n_members"] == 7
        # 2022-06-01: AAA, CCC, DDD (re-entry), FFF + GGG/HHH/III/JJJ active.
        assert by_date.loc[date(2022, 6, 1), "n_members"] == 8
        assert report.n_symbol_history_rows == 1

    def test_coverage_report_joins_prices(self, engine, tmp_path: Path) -> None:
        import pandas as pd

        provider = FixtureSP500Provider()
        run_import(provider, engine=engine, artifact_root=tmp_path, coverage_start=FIXTURE_COVERAGE_START)

        prices = pd.DataFrame(
            {
                "ticker": ["AAA", "CCC"],
                "date": [date(2022, 6, 1), date(2022, 6, 1)],
            }
        )
        report = coverage_report(engine, FIXTURE_UNIVERSE_ID, dates=[date(2022, 6, 1)], prices=prices)
        row = report.by_date.iloc[0]
        assert row["n_members"] == 8
        assert row["n_priced_members"] == 2
        assert row["n_unpriced_members"] == 6

    def test_record_rejected_batch_is_auditable(self, engine, tmp_path: Path) -> None:
        batch = record_rejected_batch(
            engine=engine,
            universe_id="sp500_fixture",
            provider_name="fixture_sp500",
            source_version="v1",
            raw_artifact_path="x",
            raw_checksum_sha256="a" * 64,
            retrieved_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            issues=["overlapping_intervals: AAA"],
        )
        assert batch.status == "rejected"
        assert "overlapping_intervals" in batch.rejected_reason


# ─── Fix round: exclusion audit + left-censored known_at ─────────────────────


class TestExclusionAudit:
    """Adversarial-review fix: --exclude-tickers must leave a DB-queryable record."""

    def test_exclusions_persisted_on_batch_row(self, engine, tmp_path: Path) -> None:
        import json

        provider = FixtureSP500Provider()
        batch = run_import(
            provider,
            engine=engine,
            artifact_root=tmp_path,
            coverage_start=FIXTURE_COVERAGE_START,
            exclude_tickers={"BBB"},
            exclude_reason="test collision exclusion",
        )
        with Session(engine) as session:
            stored = session.get(UniverseImportBatch, batch.id)
            record = json.loads(stored.excluded_tickers)
        assert record["tickers"] == ["BBB"]
        assert record["reason"] == "test collision exclusion"
        # And the excluded ticker really has no membership intervals.
        with Session(engine) as session:
            n_bbb = len(
                session.execute(
                    select(UniverseMembership).where(UniverseMembership.ticker == "BBB")
                ).scalars().all()
            )
        assert n_bbb == 0

    def test_coverage_report_surfaces_exclusions(self, engine, tmp_path: Path) -> None:
        provider = FixtureSP500Provider()
        run_import(
            provider,
            engine=engine,
            artifact_root=tmp_path,
            coverage_start=FIXTURE_COVERAGE_START,
            exclude_tickers={"BBB"},
            exclude_reason="test collision exclusion",
        )
        report = coverage_report(engine, FIXTURE_UNIVERSE_ID, dates=[date(2022, 6, 1)])
        assert report.excluded_tickers == {
            "tickers": ["BBB"],
            "reason": "test collision exclusion",
        }

    def test_no_exclusions_leaves_null_record(self, engine, tmp_path: Path) -> None:
        provider = FixtureSP500Provider()
        batch = run_import(
            provider, engine=engine, artifact_root=tmp_path, coverage_start=FIXTURE_COVERAGE_START
        )
        with Session(engine) as session:
            stored = session.get(UniverseImportBatch, batch.id)
        assert stored.excluded_tickers is None


class TestLeftCensoredKnownAt:
    """Adversarial-review fix: a left-censored member must be eligible on the
    first day of the certified coverage window (its effective_start is the
    fabricated coverage boundary, not a real change that had to become
    knowable — the next-session rule must not apply)."""

    def _left_censored_bundle(self):
        parsed = ParsedConstituentData(
            universe_id="sp500_fixture",
            current_rows=[
                CurrentConstituentRow(
                    ticker="AAA",
                    security_name="Alpha",
                    effective_start=date(2019, 1, 2),
                    source_record_id="current-AAA",
                ),
            ],
            change_events=[
                ChangeEvent(
                    effective_date=date(2020, 3, 2),
                    added_ticker=None,
                    added_security_name=None,
                    removed_ticker="ZZZ",
                    removed_security_name="Zulu",
                    reason="Market capitalization change.",
                    source_record_id="chg-zzz",
                )
            ],
        )
        bundle = build_staging_records(
            parsed, coverage_start=date(2019, 1, 2), source="fixture_sp500", source_version="v1"
        )
        return derive_known_at(bundle)

    def test_left_censored_known_at_is_coverage_start_session_close(self) -> None:
        from data.universe.calendar import session_close_cutoff

        bundle = self._left_censored_bundle()
        zzz = next(r for r in bundle.membership if r.ticker == "ZZZ")
        assert zzz.left_censored is True
        assert zzz.known_at == session_close_cutoff(date(2019, 1, 2))

    def test_left_censored_member_eligible_exactly_at_coverage_start(self, tmp_path: Path) -> None:
        from data.universe.runtime import PITUniverseLookup

        eng = create_engine(f"sqlite:///{tmp_path / 'lc.db'}", future=True)
        bundle = self._left_censored_bundle()
        publish(
            bundle,
            engine=eng,
            provider_name="fixture_sp500",
            source_version="v1",
            raw_artifact_path="x",
            raw_checksum_sha256="a" * 64,
            retrieved_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            coverage_start=date(2019, 1, 2),
            coverage_end=date(2021, 1, 4),
        )
        lookup = PITUniverseLookup(eng, "sp500_fixture")
        # Day one of the certified window: the left-censored member is IN.
        assert lookup.is_eligible("ZZZ", date(2019, 1, 2)) is True
        # An ordinary entrant (AAA, effective_start == coverage_start via the
        # current table) still follows the conservative next-session rule.
        assert lookup.is_eligible("AAA", date(2019, 1, 2)) is False
        assert lookup.is_eligible("AAA", date(2019, 1, 3)) is True


# ─── Codex PR #34 P1 fixes: exclusion side-preservation + re-import ──────────


class TestApplyExclusionsPreservesNonExcludedSide:
    """Codex P1: excluding one side of a two-sided change event must not drop
    the other side's legitimate add/remove."""

    def _two_sided_event(self) -> ParsedConstituentData:
        return ParsedConstituentData(
            universe_id="sp500_fixture",
            current_rows=[],
            change_events=[
                ChangeEvent(
                    effective_date=date(2021, 10, 10),
                    added_ticker="NEW",
                    added_security_name="New Co",
                    removed_ticker="EXCL",
                    removed_security_name="Excluded Co",
                    reason="NEW replaced EXCL.",
                    source_record_id="chg-two-sided",
                )
            ],
        )

    def test_non_excluded_added_side_survives(self) -> None:
        parsed = apply_exclusions(self._two_sided_event(), {"EXCL"})
        assert len(parsed.change_events) == 1
        evt = parsed.change_events[0]
        assert evt.added_ticker == "NEW"
        assert evt.removed_ticker is None
        assert evt.removed_security_name is None

    def test_non_excluded_removed_side_survives(self) -> None:
        parsed = apply_exclusions(self._two_sided_event(), {"NEW"})
        assert len(parsed.change_events) == 1
        evt = parsed.change_events[0]
        assert evt.added_ticker is None
        assert evt.removed_ticker == "EXCL"

    def test_fully_excluded_event_dropped(self) -> None:
        parsed = apply_exclusions(self._two_sided_event(), {"NEW", "EXCL"})
        assert parsed.change_events == []

    def test_surviving_side_gets_correct_interval_start(self, tmp_path: Path) -> None:
        # End-to-end: NEW's addition date must come from the change event,
        # not degrade to a missing or left-censored interval.
        base = self._two_sided_event()
        parsed = ParsedConstituentData(
            universe_id=base.universe_id,
            current_rows=[
                CurrentConstituentRow(
                    ticker="AAA",
                    security_name="Anchor",
                    effective_start=date(2019, 1, 2),
                    source_record_id="current-AAA",
                ),
                CurrentConstituentRow(
                    ticker="NEW",
                    security_name="New Co",
                    effective_start=date(2021, 10, 10),
                    source_record_id="current-NEW",
                ),
            ],
            change_events=base.change_events,
        )
        filtered = apply_exclusions(parsed, {"EXCL"})
        bundle = build_staging_records(
            filtered, coverage_start=date(2019, 1, 2), source="fixture_sp500", source_version="v1"
        )
        new_rows = [r for r in bundle.membership if r.ticker == "NEW"]
        assert len(new_rows) == 1
        assert new_rows[0].effective_start == date(2021, 10, 10)
        assert not any(r.ticker == "EXCL" for r in bundle.membership)


class TestReimportAdvancesCoverage:
    """Codex P1: a coverage-advancing re-import must succeed and not
    double-count against the previous published batch."""

    def test_second_import_publishes_and_scopes_reads(self, tmp_path: Path) -> None:
        eng = create_engine(f"sqlite:///{tmp_path / 'reimport.db'}", future=True)

        first = run_import(
            FixtureSP500Provider(),
            engine=eng,
            artifact_root=tmp_path / "a1",
            coverage_start=FIXTURE_COVERAGE_START,
        )
        second = run_import(
            FixtureSP500Provider(
                retrieved_at=datetime(2024, 6, 3, tzinfo=timezone.utc)
            ),
            engine=eng,
            artifact_root=tmp_path / "a2",
            coverage_start=FIXTURE_COVERAGE_START,
        )
        assert second.status == "published"
        assert second.id != first.id
        assert second.coverage_end == date(2024, 6, 3)

        # Lookup serves the newest batch and its advanced coverage.
        from data.universe.runtime import PITUniverseLookup

        lookup = PITUniverseLookup(eng, FIXTURE_UNIVERSE_ID)
        assert lookup.import_batch_id == second.id
        assert lookup.coverage_end == date(2024, 6, 3)
        # A date valid only under the new coverage works.
        assert lookup.is_eligible("AAA", date(2024, 5, 1)) is True

        # Coverage report is scoped to the newest batch: member counts do
        # not double after the re-import.
        report = coverage_report(eng, FIXTURE_UNIVERSE_ID, dates=[date(2022, 6, 1)])
        assert report.by_date.iloc[0]["n_members"] == 8
        # Symbol history was not duplicated by the second publish.
        assert report.n_symbol_history_rows == 1
