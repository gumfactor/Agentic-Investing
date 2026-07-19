"""§1.4 acceptance tests for the point-in-time universe contract (BUG-008).

One test (or test group) per bullet of docs/plans/01b-research-validity-design.md
§1.4, in document order. Some behaviors are also covered by unit tests in
test_import_pipeline.py / test_runtime.py; this file restates them as the
formal acceptance record for 01B-2 so a reviewer can check the contract off
item by item.

All data is FIXTURE data (FixtureSP500Provider — universe_id "sp500_fixture").
Interval recap:

- AAA, GGG, HHH, III, JJJ: [2020-01-01, open)
- BBB: [2020-06-01, 2021-01-01)                      (removed constituent)
- CCC: [2021-06-01, open)                            (entrant)
- DDD: [2020-01-01, 2020-04-01) + [2022-01-01, open) (remove-then-re-enter)
- EEE -> FFF renamed 2021-03-01                      (ticker change)
Coverage: [2020-01-01, 2024-01-02].
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from data.universe.calendar import is_trading_session, session_close_cutoff
from data.universe.import_pipeline import (
    MembershipCandidate,
    StagingBundle,
    build_staging_records,
    coverage_report,
    derive_known_at,
    run_import,
    validate_staging,
)
from data.universe.providers.base import ChangeEvent, ParsedConstituentData
from data.universe.providers.fixture_provider import (
    FIXTURE_COVERAGE_START,
    FIXTURE_UNIVERSE_ID,
    FixtureSP500Provider,
)
from data.universe.runtime import (
    CoverageGapError,
    CurrentUniverseRejectedError,
    CurrentUniverseSnapshot,
    InsufficientCrossSectionError,
    PITUniverseLookup,
)
from signals.research.ic import compute_ic_series


# ─── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("acceptance_db")
    eng = create_engine(f"sqlite:///{tmp / 'acceptance.db'}", future=True)
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


def _trading_days(start: date, n: int) -> list[date]:
    days: list[date] = []
    d = start
    while len(days) < n:
        if is_trading_session(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def _make_prices(tickers: list[str], dates: list[date], seed: int = 7) -> pd.DataFrame:
    """Deterministic positive price paths for every (ticker, date)."""
    rng = np.random.default_rng(seed)
    rows = []
    for ticker in tickers:
        level = 100.0 + rng.uniform(-20, 20)
        for d in dates:
            level *= 1.0 + rng.normal(0, 0.01)
            rows.append({"ticker": ticker, "date": d, "close": round(level, 4)})
    return pd.DataFrame(rows)


def _make_scores(prices: pd.DataFrame, score_dates: list[date], seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    subset = prices[prices["date"].isin(score_dates)][["ticker", "date"]].copy()
    subset["test_score"] = rng.normal(0, 1, size=len(subset))
    return subset.reset_index(drop=True)


_MEMBERS_2022 = ["AAA", "CCC", "DDD", "FFF", "GGG", "HHH", "III", "JJJ"]


# ─── Item 1: removed constituent / entrant boundary behavior ──────────────────


class TestItem1RemovedAndEntrantBoundaries:
    def test_removed_constituent_included_before_excluded_on_and_after_end(self, lookup) -> None:
        # BBB's effective_end is 2021-01-01 (exclusive interval end). The
        # exclusion applies under the knowledge cutoff: with a date-only
        # source the removal is knowable only from the next session's close
        # (conservative rule, exit-side mirror of the entry known_at rule —
        # Codex PR #34 P2), so "excluded on/after its effective end" holds
        # from the first cutoff at which the removal was knowable.
        assert lookup.is_eligible("BBB", date(2020, 12, 31)) is True
        # On the end date itself the removal is not yet knowable under the
        # default same-session cutoff: excluding it here would use future
        # information (removals correlate with declines -> upward bias).
        assert lookup.is_eligible("BBB", date(2021, 1, 1)) is True
        # With a cutoff at/after the removal's availability, excluded on end:
        assert (
            lookup.is_eligible(
                "BBB", date(2021, 1, 1),
                observation_cutoff=datetime(2021, 1, 5, tzinfo=timezone.utc),
            )
            is False
        )
        assert lookup.is_eligible("BBB", date(2021, 1, 4)) is False  # next session
        assert lookup.is_eligible("BBB", date(2021, 3, 1)) is False  # after end

    def test_entrant_excluded_before_included_after_start(self, lookup) -> None:
        # CCC effective_start 2021-06-01; date-only known_at admits it from
        # the following session under the default session-close cutoff.
        assert lookup.is_eligible("CCC", date(2021, 5, 28)) is False  # before
        assert lookup.is_eligible("CCC", date(2021, 6, 2)) is True  # after


# ─── Item 2: interval/validation semantics ────────────────────────────────────


def _bundle_with(rows: list[MembershipCandidate]) -> StagingBundle:
    bundle = StagingBundle(universe_id="sp500_fixture", coverage_start=date(2019, 1, 1))
    bundle.membership = rows
    return bundle


def _row(ticker: str, start: date, end: date | None, rid: str = "r") -> MembershipCandidate:
    return MembershipCandidate(
        ticker=ticker, vendor_symbol=None, effective_start=start, effective_end=end,
        source="fixture_sp500", source_record_id=rid, reason=None,
    )


class TestItem2IntervalSemantics:
    def test_adjacent_intervals_allowed(self) -> None:
        bundle = _bundle_with([
            _row("AAA", date(2019, 1, 1), date(2020, 1, 1), "a"),
            _row("AAA", date(2020, 1, 1), None, "b"),
        ])
        issues = validate_staging(bundle, coverage_end=date(2021, 1, 1))
        assert issues == []

    def test_remove_then_reenter_intervals_allowed(self, lookup) -> None:
        # DDD holds two disjoint stints; both import cleanly and both query.
        assert lookup.is_eligible("DDD", date(2020, 2, 1)) is True
        assert lookup.is_eligible("DDD", date(2021, 6, 1)) is False
        assert lookup.is_eligible("DDD", date(2022, 6, 1)) is True

    def test_overlapping_intervals_fail(self) -> None:
        bundle = _bundle_with([
            _row("AAA", date(2019, 1, 1), date(2020, 6, 1), "a"),
            _row("AAA", date(2020, 1, 1), None, "b"),
        ])
        issues = validate_staging(bundle, coverage_end=date(2021, 1, 1))
        assert any("overlapping_intervals" in i for i in issues)

    def test_global_coverage_gap_fails(self) -> None:
        bundle = _bundle_with([
            _row("AAA", date(2019, 1, 1), date(2019, 6, 1), "a"),
        ])
        issues = validate_staging(bundle, coverage_end=date(2020, 1, 1))
        assert any("global_coverage_gap" in i for i in issues)

    def test_future_announced_change_fails(self) -> None:
        row = _row("AAA", date(2019, 1, 1), None, "a")
        row.announced_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        bundle = _bundle_with([row])
        issues = validate_staging(
            bundle,
            coverage_end=date(2020, 1, 1),
            ingested_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        assert any("future_announced" in i for i in issues)

    def test_after_close_same_session_application_fails(self, lookup) -> None:
        # A date-only change effective 2021-06-01 gets known_at at the NEXT
        # session's close (conservative rule), so applying it on its own
        # session under the session-close cutoff is structurally impossible.
        assert lookup.is_eligible("CCC", date(2021, 6, 1)) is False
        result = lookup.load_universe_as_of(date(2021, 6, 1))
        assert "CCC" not in result.eligible_tickers
        assert any(t.ticker == "CCC" for t in result.exclusions)

    def test_unknown_symbol_mapping_fails(self) -> None:
        bundle = _bundle_with([_row("bad ticker!", date(2019, 1, 1), None, "a")])
        issues = validate_staging(bundle, coverage_end=date(2020, 1, 1))
        assert any("unknown_symbol" in i for i in issues)

    def test_per_ticker_absence_is_valid_non_membership_not_coverage_failure(
        self, lookup
    ) -> None:
        # ZZZZ was never a member: query returns False (no exception), and
        # the fixture bundle validates clean despite ZZZZ's absence.
        assert lookup.is_eligible("ZZZZ", date(2022, 6, 1)) is False
        parsed = FixtureSP500Provider().parse(FixtureSP500Provider().fetch())
        bundle = build_staging_records(
            parsed, coverage_start=FIXTURE_COVERAGE_START,
            source="fixture_sp500", source_version="v1",
        )
        bundle = derive_known_at(bundle)
        assert validate_staging(bundle, coverage_end=date(2024, 1, 1)) == []


# ─── Item 3: IC inputs exclude priced-but-not-member tickers ──────────────────


class TestItem3ICExcludesNonMembers:
    def test_priced_non_member_rows_do_not_affect_ic(self, lookup) -> None:
        dates = _trading_days(date(2022, 2, 1), 40)
        score_dates = dates[:10]

        # XXX has full prices and scores but zero membership intervals.
        prices_with = _make_prices(_MEMBERS_2022 + ["XXX"], dates)
        scores_with = _make_scores(prices_with, score_dates)

        prices_without = prices_with[prices_with["ticker"] != "XXX"].reset_index(drop=True)
        scores_without = scores_with[scores_with["ticker"] != "XXX"].reset_index(drop=True)

        ic_with = compute_ic_series(
            scores_with, prices_with, "test_score", horizons=[5], universe=lookup
        )
        ic_without = compute_ic_series(
            scores_without, prices_without, "test_score", horizons=[5], universe=lookup
        )
        pd.testing.assert_frame_equal(ic_with, ic_without)
        assert not ic_with.empty
        # Cross-section size equals the member count exactly (XXX excluded).
        assert (ic_with["n_obs"] == len(_MEMBERS_2022)).all()

    def test_removed_constituent_excluded_from_ic_after_removal(self, lookup) -> None:
        # BBB is priced throughout but a member only until 2021-01-01; on
        # 2022 score dates its rows must not enter the cross-section.
        dates = _trading_days(date(2022, 2, 1), 40)
        prices = _make_prices(_MEMBERS_2022 + ["BBB"], dates)
        scores = _make_scores(prices, dates[:5])
        ic = compute_ic_series(scores, prices, "test_score", horizons=[5], universe=lookup)
        assert (ic["n_obs"] == len(_MEMBERS_2022)).all()

    def test_ic_fails_closed_outside_coverage(self, lookup) -> None:
        dates = _trading_days(date(2025, 2, 3), 40)  # after coverage_end 2024-01-02
        prices = _make_prices(_MEMBERS_2022, dates)
        scores = _make_scores(prices, dates[:5])
        with pytest.raises(CoverageGapError):
            compute_ic_series(scores, prices, "test_score", horizons=[5], universe=lookup)


# ─── Item 4: current-universe loader rejected by historical code ──────────────


class TestItem4CurrentUniverseRejected:
    def _data(self):
        dates = _trading_days(date(2022, 2, 1), 40)
        prices = _make_prices(_MEMBERS_2022, dates)
        scores = _make_scores(prices, dates[:5])
        return scores, prices

    def test_current_universe_snapshot_rejected_by_ic(self) -> None:
        scores, prices = self._data()
        snap = CurrentUniverseSnapshot(
            operational_tickers=tuple(_MEMBERS_2022),
            fetched_at=datetime.now(tz=timezone.utc),
            source="test",
        )
        with pytest.raises(CurrentUniverseRejectedError):
            compute_ic_series(scores, prices, "test_score", horizons=[5], universe=snap)

    def test_plain_ticker_list_rejected_by_ic(self) -> None:
        scores, prices = self._data()
        with pytest.raises(CurrentUniverseRejectedError):
            compute_ic_series(
                scores, prices, "test_score", horizons=[5], universe=_MEMBERS_2022
            )


# ─── Item 5: coverage report reconciliation + insufficient cross-section ──────


class TestItem5CoverageReportAndCrossSection:
    def test_coverage_report_reconciles_members_prices_exclusions(self, engine, lookup) -> None:
        dates = [date(2022, 6, 1)]
        prices = pd.DataFrame(
            {"ticker": ["AAA", "GGG", "XXX"], "date": [date(2022, 6, 1)] * 3}
        )
        report = coverage_report(engine, FIXTURE_UNIVERSE_ID, dates=dates, prices=prices)
        row = report.by_date.iloc[0]
        # Members reconcile with the runtime lookup...
        universe = lookup.load_universe_as_of(date(2022, 6, 1))
        assert row["n_members"] == len(universe.eligible_tickers)
        # ...priced + unpriced partition the membership exactly (XXX, a
        # priced non-member, is not counted anywhere).
        assert row["n_priced_members"] == 2
        assert row["n_unpriced_members"] == row["n_members"] - 2

    def test_insufficient_cross_section_fails_instead_of_shrunken_ic(self, lookup) -> None:
        dates = _trading_days(date(2022, 2, 1), 40)
        # Only 4 members scored: below the 5-ticker minimum cross-section.
        prices = _make_prices(_MEMBERS_2022[:4], dates)
        scores = _make_scores(prices, dates[:5])
        with pytest.raises(InsufficientCrossSectionError):
            compute_ic_series(scores, prices, "test_score", horizons=[5], universe=lookup)

    def test_cross_section_entirely_non_member_fails_not_silently_dropped(self, lookup) -> None:
        dates = _trading_days(date(2022, 2, 1), 40)
        # Every scored ticker is priced but was never a member.
        prices = _make_prices(["XX1", "XX2", "XX3", "XX4", "XX5", "XX6"], dates)
        scores = _make_scores(prices, dates[:5])
        with pytest.raises(InsufficientCrossSectionError):
            compute_ic_series(scores, prices, "test_score", horizons=[5], universe=lookup)


# ─── Codex PR #34 round 2: removal known_at gating ────────────────────────────


class TestRemovalKnownAtGating:
    """A removal effective on session d (date-only source) is knowable only
    from the next session's close; until then the ticker remains eligible.
    Exit-side mirror of the entry known_at rule."""

    def test_removal_not_applied_before_knowable(self, lookup) -> None:
        # BBB removed effective 2021-01-01; next trading session 2021-01-04.
        assert lookup.is_eligible("BBB", date(2021, 1, 1)) is True

    def test_removal_applied_from_first_knowable_cutoff(self, lookup) -> None:
        # Cutoff exactly at the removal's availability (2021-01-04 close):
        # end_known_at > cutoff is False, so the removal applies.
        from data.universe.calendar import next_trading_session

        knowable_at = session_close_cutoff(next_trading_session(date(2021, 1, 1)))
        assert (
            lookup.is_eligible("BBB", date(2021, 1, 1), observation_cutoff=knowable_at)
            is False
        )

    def test_load_universe_as_of_includes_pending_removal(self, lookup) -> None:
        result = lookup.load_universe_as_of(date(2021, 1, 1))
        assert "BBB" in result.eligible_tickers

    def test_load_universe_as_of_excludes_once_knowable(self, lookup) -> None:
        result = lookup.load_universe_as_of(
            date(2021, 1, 1),
            observation_cutoff=datetime(2021, 1, 5, tzinfo=timezone.utc),
        )
        assert "BBB" not in result.eligible_tickers

    def test_entry_rule_still_enforced_alongside_exit_rule(self, lookup) -> None:
        # CCC added effective 2021-06-01: still not eligible on its own
        # effective-start session (entry known_at rule unchanged).
        assert lookup.is_eligible("CCC", date(2021, 6, 1)) is False


# ─── Codex PR #34 round 5: date-dtype normalization at PIT boundaries ────────


class TestTimestampDateNormalization:
    """pandas Timestamp/datetime64 dates (read_sql/CSV/parquet loaders) must
    work identically to plain datetime.date at every PIT boundary."""

    def test_ic_with_timestamp_dates_equals_plain_dates(self, lookup) -> None:
        dates = _trading_days(date(2022, 2, 1), 40)
        prices = _make_prices(_MEMBERS_2022, dates)
        scores = _make_scores(prices, dates[:5])

        ic_plain = compute_ic_series(scores, prices, "test_score", horizons=[5], universe=lookup)

        prices_ts = prices.copy()
        prices_ts["date"] = pd.to_datetime(prices_ts["date"])
        scores_ts = scores.copy()
        scores_ts["date"] = pd.to_datetime(scores_ts["date"])
        ic_ts = compute_ic_series(scores_ts, prices_ts, "test_score", horizons=[5], universe=lookup)

        ic_ts_norm = ic_ts.copy()
        ic_ts_norm["score_date"] = pd.to_datetime(ic_ts_norm["score_date"]).dt.date
        pd.testing.assert_frame_equal(ic_plain, ic_ts_norm)

    def test_runtime_accepts_timestamp_as_of(self, lookup) -> None:
        plain = lookup.load_universe_as_of(date(2022, 6, 1))
        via_ts = lookup.load_universe_as_of(pd.Timestamp("2022-06-01"))
        assert via_ts.eligible_tickers == plain.eligible_tickers
        assert lookup.is_eligible("AAA", pd.Timestamp("2022-06-01")) is True

    def test_runtime_rejects_non_datelike(self, lookup) -> None:
        with pytest.raises(TypeError):
            lookup.load_universe_as_of("not-a-date-at-all!")
