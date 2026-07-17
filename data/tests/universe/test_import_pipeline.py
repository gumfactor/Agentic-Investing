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
        # 2020-03-01: AAA, DDD (first stint), EEE are active.
        assert by_date.loc[date(2020, 3, 1), "n_members"] == 3
        # 2020-07-01: AAA, BBB, EEE active (DDD stint 1 closed 04-01).
        assert by_date.loc[date(2020, 7, 1), "n_members"] == 3
        # 2022-06-01: AAA, CCC, DDD (re-entry), FFF active.
        assert by_date.loc[date(2022, 6, 1), "n_members"] == 4
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
        assert row["n_members"] == 4
        assert row["n_priced_members"] == 2
        assert row["n_unpriced_members"] == 2

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
