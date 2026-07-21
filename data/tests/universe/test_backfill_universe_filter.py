"""Tests for the PIT membership filter in scripts/backfill_momentum_scores.py.

Uses the fixture universe (sp500_fixture) with an injected PITUniverseLookup
and mocked snapshots — no live DB, no MinIO.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from data.universe.calendar import is_trading_session
from data.universe.import_pipeline import run_import
from data.universe.providers.fixture_provider import (
    FIXTURE_COVERAGE_START,
    FIXTURE_UNIVERSE_ID,
    FixtureSP500Provider,
)
from data.storage.errors import SnapshotNotFoundError
from data.universe.runtime import CoverageGapError, PITUniverseLookup
from scripts.backfill_momentum_scores import _parse_args, run


@pytest.fixture(scope="module")
def lookup(tmp_path_factory) -> PITUniverseLookup:
    tmp = tmp_path_factory.mktemp("backfill_db")
    eng = create_engine(f"sqlite:///{tmp / 'u.db'}", future=True)
    run_import(
        FixtureSP500Provider(),
        engine=eng,
        artifact_root=tmp / "artifacts",
        coverage_start=FIXTURE_COVERAGE_START,
    )
    return PITUniverseLookup(eng, FIXTURE_UNIVERSE_ID)


def _make_prices(tickers: list[str], start: date, n_days: int) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    dates: list[date] = []
    d = start
    while len(dates) < n_days:
        if is_trading_session(d):
            dates.append(d)
        d += timedelta(days=1)
    rows = []
    for t in tickers:
        level = 100.0
        for dd in dates:
            level *= 1.0 + rng.normal(0.0003, 0.01)
            rows.append({"ticker": t, "date": pd.Timestamp(dd), "close": round(level, 4)})
    return pd.DataFrame(rows)


def _mock_snapshots(prices: pd.DataFrame) -> MagicMock:
    """A ParquetSnapshots double distinguishing data_type — BUG-009 P0 fix
    wiring means run() now also loads a "corporate_actions" snapshot; a
    single-return_value mock would answer that call with the prices frame
    (missing ex_date/known_at) and crash. No corporate_actions snapshot is
    pinned in these fixtures, so the call raises SnapshotNotFoundError
    (03A-2; subclasses FileNotFoundError for one deprecation cycle),
    matching the real ParquetSnapshots behavior for a snapshot that was
    never saved — run() degrades to raw (unadjusted) prices with a logged
    warning."""
    mock_snaps = MagicMock()

    # 03A-1: scripts/backfill_momentum_scores.py reads pre-03A-1 date-keyed
    # snapshots via ParquetSnapshots.load_snapshot_legacy(data_type, date)
    # (load_snapshot's second arg is now a content hash). This is a date-keyed
    # legacy read path, unchanged in behavior -- only the method name moved.
    def _load_snapshot(data_type: str, snapshot_date: date):
        if data_type == "daily_prices":
            return prices
        raise SnapshotNotFoundError(f"no snapshot pinned for {data_type!r}")

    mock_snaps.load_snapshot_legacy.side_effect = _load_snapshot
    return mock_snaps


class TestBackfillPITFilter:
    def test_non_member_scores_are_filtered_out(self, lookup, capsys) -> None:
        # 273+ lookback days before the score window; XXX is priced but never
        # a member of the fixture universe.
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members + ["XXX"], date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()

        mock_snaps = _mock_snapshots(prices)

        run(
            snapshot_date=date(2024, 1, 2),
            start=start,
            end=end,
            strategy_id="vtest",
            batch_size=20,
            dry_run=True,
            snapshots=mock_snaps,
            universe_id=FIXTURE_UNIVERSE_ID,
            universe_lookup=lookup,
        )
        out = capsys.readouterr().out
        assert "[DRY RUN]" in out
        assert "XXX" not in out  # non-member never reaches the output

    def test_fails_closed_outside_coverage(self, lookup) -> None:
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        # Score window extends beyond fixture coverage_end (2024-01-02).
        prices = _make_prices(members, date(2023, 1, 2), 273 + 60)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()
        assert end > date(2024, 1, 2)

        mock_snaps = _mock_snapshots(prices)

        with pytest.raises(CoverageGapError):
            run(
                snapshot_date=date(2024, 1, 2),
                start=start,
                end=end,
                strategy_id="vtest",
                batch_size=20,
                dry_run=True,
                snapshots=mock_snaps,
                universe_id=FIXTURE_UNIVERSE_ID,
                universe_lookup=lookup,
            )


class TestPITCrossSectionBeforeZScore:
    """Codex PR #34 P1: membership must define the scoring cross-section
    BEFORE z-scoring — a priced non-member must not be able to shift the
    cross-sectional mean/std (and therefore members' persisted scores)."""

    def _prices_and_eligibility(self):
        import numpy as np

        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        base = _make_prices(members, date(2021, 1, 4), 273 + 40)

        # WHALE: same calendar, deliberately extreme trend so its raw
        # momentum return would drastically shift any mean/std it enters.
        whale_dates = sorted(base["date"].unique())
        whale_rows = []
        level = 10.0
        for d in whale_dates:
            level *= 1.05  # ~5% per day — an outlier by construction
            whale_rows.append({"ticker": "WHALE", "date": d, "close": round(level, 6)})
        import pandas as pd

        prices_with = pd.concat([base, pd.DataFrame(whale_rows)], ignore_index=True)
        prices_without = base

        window_dates = [d.date() for d in whale_dates]
        eligibility = pd.DataFrame(
            [{"ticker": t, "date": d} for d in window_dates for t in members]
        )
        return prices_with, prices_without, eligibility, members

    def test_non_member_cannot_shift_member_z_scores(self) -> None:
        import pandas as pd

        from signals.composites.momentum_score import compute_momentum_scores

        prices_with, prices_without, eligibility, members = self._prices_and_eligibility()

        with_whale = compute_momentum_scores(prices_with, eligibility=eligibility)
        without_whale = compute_momentum_scores(prices_without, eligibility=eligibility)

        w = (
            with_whale[with_whale["ticker"].isin(members)]
            .sort_values(["ticker", "date"])
            .reset_index(drop=True)
        )
        wo = (
            without_whale[without_whale["ticker"].isin(members)]
            .sort_values(["ticker", "date"])
            .reset_index(drop=True)
        )
        pd.testing.assert_frame_equal(w, wo)

    def test_non_member_rows_never_scored(self) -> None:
        from signals.composites.momentum_score import compute_momentum_scores

        prices_with, _, eligibility, _ = self._prices_and_eligibility()
        scores = compute_momentum_scores(prices_with, eligibility=eligibility)
        assert not (scores["ticker"] == "WHALE").any()

    def test_without_eligibility_the_whale_contaminates(self) -> None:
        # Sanity check that the mask is doing real work: WITHOUT the
        # eligibility cross-section, the whale's extreme return shifts
        # members' z-scores (the pre-fix contamination Codex flagged).
        import numpy as np

        from signals.composites.momentum_score import compute_momentum_scores

        prices_with, prices_without, eligibility, members = self._prices_and_eligibility()

        contaminated = compute_momentum_scores(prices_with)  # legacy: no mask
        clean = compute_momentum_scores(prices_without)

        merged = contaminated.merge(
            clean, on=["ticker", "date"], suffixes=("_dirty", "_clean")
        )
        merged = merged[merged["ticker"].isin(members)]
        valid = merged.dropna(subset=["momentum_score_dirty", "momentum_score_clean"])
        assert len(valid) > 0
        assert not np.allclose(
            valid["momentum_score_dirty"], valid["momentum_score_clean"]
        )


# ─── Corporate-action cutoff-aware wiring (BUG-009 P0 fix) ────────────────────

class TestBackfillCorporateActionWiring:
    """Verifies scripts.backfill_momentum_scores.run() actually feeds a
    split-adjusted price series into compute_momentum_scores rather than
    silently passing raw prices through (the P0 adversarial-review finding:
    the cutoff-aware builders existed but had no production caller)."""

    def _mock_snapshots_with_actions(self, prices: pd.DataFrame, corporate_actions: pd.DataFrame) -> MagicMock:
        mock_snaps = MagicMock()

        def _load_snapshot(data_type: str, snapshot_date: date):
            if data_type == "daily_prices":
                return prices
            if data_type == "corporate_actions":
                return corporate_actions
            raise SnapshotNotFoundError(f"no snapshot pinned for {data_type!r}")

        mock_snaps.load_snapshot_legacy.side_effect = _load_snapshot
        return mock_snaps

    def test_split_adjusted_prices_reach_compute_momentum_scores(self, lookup, monkeypatch) -> None:
        from datetime import datetime, timezone

        import signals.composites.momentum_score as momentum_module

        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        all_dates = sorted(prices["date"].dt.date.unique())
        start = all_dates[273]
        end = all_dates[-1]

        # A 2-for-1 split on AAA effective (and known) well before `start`,
        # so it must be applied to AAA's entire pre-start lookback history.
        split_ex_date = all_dates[100]
        corporate_actions = pd.DataFrame(
            [
                {
                    "ticker": "AAA",
                    "ex_date": split_ex_date,
                    "action_type": "split",
                    "value": 2.0,
                    "known_at": datetime.combine(
                        all_dates[101], datetime.min.time(), tzinfo=timezone.utc
                    ),
                    "source_version": "test-v1",
                }
            ]
        )

        pre_split_date = all_dates[50]  # strictly before the split's ex_date
        # Captured BEFORE run() — run() mutates the prices DataFrame's `date`
        # column in place (Timestamp -> date), so this must be read first.
        raw_row = prices[(prices["ticker"] == "AAA") & (prices["date"].dt.date == pre_split_date)]
        raw_close = float(raw_row.iloc[0]["close"])
        raw_other = prices[(prices["ticker"] == "GGG") & (prices["date"].dt.date == pre_split_date)]
        raw_other_close = float(raw_other.iloc[0]["close"])

        captured: dict = {}
        real_compute = momentum_module.compute_momentum_scores

        def _spy(prices_arg, *args, **kwargs):
            captured["prices"] = prices_arg.copy()
            return real_compute(prices_arg, *args, **kwargs)

        monkeypatch.setattr(momentum_module, "compute_momentum_scores", _spy)

        mock_snaps = self._mock_snapshots_with_actions(prices, corporate_actions)

        run(
            snapshot_date=date(2024, 1, 2),
            start=start,
            end=end,
            strategy_id="vtest",
            batch_size=20,
            dry_run=True,
            snapshots=mock_snaps,
            universe_id=FIXTURE_UNIVERSE_ID,
            universe_lookup=lookup,
        )

        assert "prices" in captured, "compute_momentum_scores was never called"
        adjusted = captured["prices"]

        adj_row = adjusted[(adjusted["ticker"] == "AAA") & (adjusted["date"] == pre_split_date)]
        assert not adj_row.empty
        adj_close = float(adj_row.iloc[0]["close"])
        # Split-adjusted: pre-split close must be roughly half the raw close
        # (the whole point of the P0 wiring fix) — not equal to it.
        assert abs(adj_close - raw_close / 2.0) < 1e-6
        assert abs(adj_close - raw_close) > 1.0

        # A different ticker with no corporate actions is untouched.
        adj_other = adjusted[(adjusted["ticker"] == "GGG") & (adjusted["date"] == pre_split_date)]
        assert abs(float(adj_other.iloc[0]["close"]) - raw_other_close) < 1e-6

    def test_missing_corporate_actions_snapshot_degrades_to_raw_prices(self, lookup) -> None:
        """No corporate_actions snapshot pinned (FileNotFoundError) must not
        crash the backfill — it degrades to raw (unadjusted) prices, matching
        pre-01B-3 behavior, with a logged warning."""
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()

        mock_snaps = _mock_snapshots(prices)

        # Should not raise.
        run(
            snapshot_date=date(2024, 1, 2),
            start=start,
            end=end,
            strategy_id="vtest",
            batch_size=20,
            dry_run=True,
            snapshots=mock_snaps,
            universe_id=FIXTURE_UNIVERSE_ID,
            universe_lookup=lookup,
        )

    def test_pre_migration_011_snapshot_synthesizes_known_at(self, lookup, monkeypatch) -> None:
        """BUG-009 P2 (adversarial review round 3): a corporate_actions
        snapshot pinned before migration 011 has no known_at/source_version
        columns at all. run() must not raise KeyError -- it synthesizes
        known_at via the same conservative next-session rule migration 011
        used, and the resulting adjustment is still applied."""
        import signals.composites.momentum_score as momentum_module

        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        all_dates = sorted(prices["date"].dt.date.unique())
        start = all_dates[273]
        end = all_dates[-1]
        split_ex_date = all_dates[100]

        # Legacy snapshot shape: no known_at, no source_version columns.
        legacy_corporate_actions = pd.DataFrame(
            [
                {
                    "ticker": "AAA",
                    "ex_date": split_ex_date,
                    "action_type": "split",
                    "value": 2.0,
                }
            ]
        )

        pre_split_date = all_dates[50]
        raw_row = prices[(prices["ticker"] == "AAA") & (prices["date"].dt.date == pre_split_date)]
        raw_close = float(raw_row.iloc[0]["close"])

        captured: dict = {}
        real_compute = momentum_module.compute_momentum_scores

        def _spy(prices_arg, *args, **kwargs):
            captured["prices"] = prices_arg.copy()
            return real_compute(prices_arg, *args, **kwargs)

        monkeypatch.setattr(momentum_module, "compute_momentum_scores", _spy)

        mock_snaps = self._mock_snapshots_with_actions(prices, legacy_corporate_actions)

        # Must not raise KeyError.
        run(
            snapshot_date=date(2024, 1, 2),
            start=start,
            end=end,
            strategy_id="vtest",
            batch_size=20,
            dry_run=True,
            snapshots=mock_snaps,
            universe_id=FIXTURE_UNIVERSE_ID,
            universe_lookup=lookup,
        )

        adjusted = captured["prices"]
        adj_row = adjusted[(adjusted["ticker"] == "AAA") & (adjusted["date"] == pre_split_date)]
        adj_close = float(adj_row.iloc[0]["close"])
        # The synthesized known_at (next session after split_ex_date) is
        # still well before `end`, so the split is still applied.
        assert abs(adj_close - raw_close / 2.0) < 1e-6


class TestBackfillArgparseResearchRunId:
    """Codex round-4 P2: --dry-run never persists (research_run_id=None is
    explicitly allowed in that code path), so --research-run-id must not be
    a required CLI argument -- only run()'s actual write path enforces it."""

    def test_dry_run_does_not_require_research_run_id(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sys.argv",
            [
                "backfill_momentum_scores.py",
                "--snapshot-date", "2026-06-10",
                "--start", "2020-01-02",
                "--end", "2020-06-30",
                "--strategy-id", "v1",
                "--dry-run",
            ],
        )
        args = _parse_args()  # must not raise / exit
        assert args.research_run_id is None
        assert args.dry_run is True

    def test_research_run_id_still_accepted_when_provided(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sys.argv",
            [
                "backfill_momentum_scores.py",
                "--snapshot-date", "2026-06-10",
                "--start", "2020-01-02",
                "--end", "2020-06-30",
                "--strategy-id", "v1",
                "--research-run-id", "42",
            ],
        )
        args = _parse_args()
        assert args.research_run_id == 42
        assert args.allow_raw_prices_on_missing_actions is False


class TestMissingActionsFailsClosedOnLiveWrite:
    """Adversarial-review round 9 (BUG-009): a missing corporate_actions
    snapshot used to silently degrade to raw (unadjusted) prices and let a
    LIVE write proceed -- persisting scores under a research_run_id whose
    registered methodology (score_cutoff_known_at_v1) falsely claims
    cutoff-adjustment was applied. The same "provenance lies about what
    actually happened" pattern as the original P0 finding, reached via a
    silent degrade instead of missing wiring. dry_run stays permissive
    (preview only, never persists); a live write must fail closed unless the
    caller explicitly opts in AND supplies a research_run_id whose
    methodology honestly does not claim cutoff adjustment.
    """

    def _engine_with_methodology(
        self,
        tmp_path,
        monkeypatch,
        score_action_availability_policy,
        name,
        universe_import_policy="pit_universe_effective_dated_v1",
    ):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from data.research.identity import MethodologySpec, activate_run, register_methodology, register_run
        from data.research.models import Base

        db_path = tmp_path / f"{name}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            methodology = register_methodology(
                session,
                MethodologySpec(
                    name=name,
                    universe_import_policy=universe_import_policy,
                    timing_policy_id="t_plus_1_close_v1",
                    score_action_availability_policy=score_action_availability_policy,
                    realized_return_action_availability_policy=score_action_availability_policy,
                    action_source_version="unknown",
                    return_adjustment_policy="total_return_adjusted_v1",
                    missing_data_policy="pct_change_fill_none_v1",
                    code_config_hash="test-hash",
                ),
            )
            session.commit()
            run_row = register_run(session, methodology.id, data_version="2026-01-01")
            session.commit()
            activate_run(session, run_row.id, activated_by="test")
            session.commit()
            return run_row.id

    def test_live_write_without_opt_in_raises_before_any_db_write(self, lookup) -> None:
        # Uses the PIT lookup fixture (not --provisional-no-universe) so this
        # test exercises ONLY the round-9 corporate_actions gate, not the
        # round-10 --provisional-no-universe honesty gate added later.
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()
        mock_snaps = _mock_snapshots(prices)

        with pytest.raises(RuntimeError, match="corporate_actions snapshot is missing"):
            run(
                snapshot_date=date(2024, 1, 2),
                start=start,
                end=end,
                strategy_id="vtest",
                batch_size=20,
                dry_run=False,
                research_run_id=None,
                snapshots=mock_snaps,
                universe_id=FIXTURE_UNIVERSE_ID,
                universe_lookup=lookup,
            )

    def test_live_write_with_opt_in_but_no_research_run_id_raises(self, lookup) -> None:
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()
        mock_snaps = _mock_snapshots(prices)

        with pytest.raises(ValueError, match="requires --research-run-id"):
            run(
                snapshot_date=date(2024, 1, 2),
                start=start,
                end=end,
                strategy_id="vtest",
                batch_size=20,
                dry_run=False,
                research_run_id=None,
                snapshots=mock_snaps,
                universe_id=FIXTURE_UNIVERSE_ID,
                universe_lookup=lookup,
                allow_raw_prices_on_missing_actions=True,
            )

    def test_live_write_with_opt_in_and_dishonest_methodology_raises(
        self, tmp_path, monkeypatch, lookup
    ) -> None:
        run_id = self._engine_with_methodology(
            tmp_path, monkeypatch, "score_cutoff_known_at_v1", "dishonest_cutoff_claim"
        )
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()
        mock_snaps = _mock_snapshots(prices)

        from data.research.sql_compat import MethodologyHonestyError

        with pytest.raises(MethodologyHonestyError, match="misrepresent"):
            run(
                snapshot_date=date(2024, 1, 2),
                start=start,
                end=end,
                strategy_id="vtest",
                batch_size=20,
                dry_run=False,
                research_run_id=run_id,
                snapshots=mock_snaps,
                universe_id=FIXTURE_UNIVERSE_ID,
                universe_lookup=lookup,
                allow_raw_prices_on_missing_actions=True,
            )

    def test_dry_run_remains_permissive_without_opt_in(self, lookup) -> None:
        """Backward compatibility: --dry-run never persists, so it must stay
        permissive on a missing snapshot exactly as before round 9."""
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()
        mock_snaps = _mock_snapshots(prices)

        # Must not raise.
        run(
            snapshot_date=date(2024, 1, 2),
            start=start,
            end=end,
            strategy_id="vtest",
            batch_size=20,
            dry_run=True,
            snapshots=mock_snaps,
            universe_id=FIXTURE_UNIVERSE_ID,
            universe_lookup=lookup,
        )


class TestProvisionalNoUniverseFailsClosedOnLiveWrite:
    """Adversarial-review round 10 (BUG-008/BUG-009): --provisional-no-universe
    correctly skips PIT membership filtering, but nothing stopped an operator
    from tagging the resulting current-membership (survivorship-biased) rows
    with a research_run_id whose registered methodology
    (universe_import_policy="pit_universe_effective_dated_v1") claims
    PIT-universe safety -- downstream readers filter solely by
    research_run_id (BUG-072) and would silently consume those rows
    believing them PIT-safe. Same honesty-check pattern as round 9, on the
    universe dimension instead of the corporate-action dimension.
    """

    def _engine_with_methodology(self, tmp_path, monkeypatch, universe_import_policy, name):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from data.research.identity import MethodologySpec, activate_run, register_methodology, register_run
        from data.research.models import Base

        db_path = tmp_path / f"{name}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            methodology = register_methodology(
                session,
                MethodologySpec(
                    name=name,
                    universe_import_policy=universe_import_policy,
                    timing_policy_id="t_plus_1_close_v1",
                    score_action_availability_policy="raw_unadjusted_no_corporate_action_data",
                    realized_return_action_availability_policy="raw_unadjusted_no_corporate_action_data",
                    action_source_version="unknown",
                    return_adjustment_policy="total_return_adjusted_v1",
                    missing_data_policy="pct_change_fill_none_v1",
                    code_config_hash="test-hash",
                ),
            )
            session.commit()
            run_row = register_run(session, methodology.id, data_version="2026-01-01")
            session.commit()
            activate_run(session, run_row.id, activated_by="test")
            session.commit()
            return run_row.id

    def test_live_write_without_research_run_id_raises(self) -> None:
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()
        mock_snaps = _mock_snapshots(prices)

        with pytest.raises(ValueError, match="requires --research-run-id"):
            run(
                snapshot_date=date(2024, 1, 2),
                start=start,
                end=end,
                strategy_id="vtest",
                batch_size=20,
                dry_run=False,
                research_run_id=None,
                snapshots=mock_snaps,
                provisional_no_universe=True,
            )

    def test_live_write_tagged_with_pit_claiming_run_raises(self, tmp_path, monkeypatch) -> None:
        run_id = self._engine_with_methodology(
            tmp_path, monkeypatch, "pit_universe_effective_dated_v1", "dishonest_pit_claim"
        )
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()
        mock_snaps = _mock_snapshots(prices)

        from data.research.sql_compat import MethodologyHonestyError

        with pytest.raises(MethodologyHonestyError, match="misrepresent"):
            run(
                snapshot_date=date(2024, 1, 2),
                start=start,
                end=end,
                strategy_id="vtest",
                batch_size=20,
                dry_run=False,
                research_run_id=run_id,
                snapshots=mock_snaps,
                provisional_no_universe=True,
                # This run's methodology honestly declares no corporate-action
                # adjustment (see _engine_with_methodology's fixed
                # score_action_availability_policy), so the round-9
                # corporate_actions gate itself would pass; the explicit
                # opt-in is still required to get past ITS OWN unconditional
                # "did the operator really mean this" check so the test
                # reaches the universe-honesty violation this test is
                # actually about, not an unrelated missing-opt-in error.
                allow_raw_prices_on_missing_actions=True,
            )

    def test_live_write_tagged_with_honest_run_proceeds_past_the_gate(
        self, tmp_path, monkeypatch
    ) -> None:
        """An honestly-tagged (non-PIT-claiming) run must not be blocked by
        this gate -- it should proceed to the next stage of run() (the
        corporate_actions gate, round 9), not fail here."""
        run_id = self._engine_with_methodology(
            tmp_path, monkeypatch, "legacy_current_membership_no_pit_enforcement", "honest_no_pit"
        )
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()
        mock_snaps = _mock_snapshots(prices)

        # Passes the round-10 universe-honesty gate, then hits the round-9
        # corporate_actions gate (no snapshot pinned in this fixture) --
        # proves the round-10 gate itself did not block an honest run.
        with pytest.raises(RuntimeError, match="corporate_actions snapshot is missing"):
            run(
                snapshot_date=date(2024, 1, 2),
                start=start,
                end=end,
                strategy_id="vtest",
                batch_size=20,
                dry_run=False,
                research_run_id=run_id,
                snapshots=mock_snaps,
                provisional_no_universe=True,
            )

    def test_dry_run_remains_permissive_without_research_run_id(self) -> None:
        """--dry-run never persists, so the honesty gate must not block it
        even with no --research-run-id supplied."""
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()
        mock_snaps = _mock_snapshots(prices)

        # Must not raise.
        run(
            snapshot_date=date(2024, 1, 2),
            start=start,
            end=end,
            strategy_id="vtest",
            batch_size=20,
            dry_run=True,
            snapshots=mock_snaps,
            provisional_no_universe=True,
        )


class TestStrategyConfigEligibilityWiring:
    """03A-4b: --strategy-config wires data.universe.runtime's combined
    membership+eligibility check into the scoring path (design doc §1.3:
    "no caller can apply one check without the other")."""

    @pytest.fixture
    def eligibility_engine(self, tmp_path):
        from data.universe.eligibility_batch import write_price_eligibility_batch

        eng = create_engine(f"sqlite:///{tmp_path / 'eligibility_wiring.db'}", future=True)
        run_import(
            FixtureSP500Provider(),
            engine=eng,
            artifact_root=tmp_path / "artifacts",
            coverage_start=FIXTURE_COVERAGE_START,
        )
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        prices["date"] = prices["date"].dt.date
        prices["volume"] = 1_000_000
        write_price_eligibility_batch(
            eng,
            FIXTURE_UNIVERSE_ID,
            prices,
            start=prices["date"].min(),
            end=prices["date"].max(),
            code_version="test",
        )
        return eng, prices

    def _write_strategy_config(self, tmp_path, eligibility_block: dict) -> str:
        import yaml

        path = tmp_path / "strategy.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"universe": {"eligibility": eligibility_block}}, f)
        return str(path)

    def test_eligibility_filter_actually_excludes_a_ticker(self, tmp_path, capsys):
        """Deterministic partial-exclusion proof: two tickers are given a
        far larger trading volume than the rest so adv_usd_20d cleanly
        separates them regardless of the random-walk price noise, then a
        threshold between the two groups is asserted to exclude exactly the
        low-volume tickers from the scoring output (not just "doesn't
        crash", the failure mode a membership-only regression would have)."""
        from data.universe.eligibility_batch import write_price_eligibility_batch
        from data.universe.runtime import PITEligibilityLookup, PITUniverseLookup

        eng = create_engine(f"sqlite:///{tmp_path / 'partial_exclusion.db'}", future=True)
        run_import(
            FixtureSP500Provider(),
            engine=eng,
            artifact_root=tmp_path / "artifacts",
            coverage_start=FIXTURE_COVERAGE_START,
        )
        low_volume = ["AAA", "GGG"]
        high_volume = ["HHH", "III", "JJJ"]
        prices = _make_prices(low_volume + high_volume, date(2021, 1, 4), 273 + 40)
        prices["date"] = prices["date"].dt.date
        prices["volume"] = prices["ticker"].map(
            {"AAA": 100_000, "GGG": 300_000, "HHH": 3_000_000, "III": 4_000_000, "JJJ": 5_000_000}
        )
        write_price_eligibility_batch(
            eng,
            FIXTURE_UNIVERSE_ID,
            prices,
            start=prices["date"].min(),
            end=prices["date"].max(),
            code_version="test",
        )
        # Prices random-walk within roughly [70, 200] over the window; even
        # at the extremes, low-volume ADV (<= 300k * 200 = 60M) never
        # approaches high-volume ADV (>= 3M * 70 = 210M), so a threshold of
        # 100M cleanly separates the two groups on every date.
        strategy_config_path = self._write_strategy_config(
            tmp_path, {"adv_usd_20d": {"op": "gte", "threshold": 100_000_000.0}}
        )
        start = sorted(prices["date"].unique())[273]
        end = prices["date"].max()
        prices_for_snapshot = prices.assign(date=pd.to_datetime(prices["date"]))
        mock_snaps = _mock_snapshots(prices_for_snapshot)

        run(
            snapshot_date=date(2024, 1, 2),
            start=start,
            end=end,
            strategy_id="vtest",
            batch_size=20,
            dry_run=True,
            snapshots=mock_snaps,
            universe_id=FIXTURE_UNIVERSE_ID,
            universe_lookup=PITUniverseLookup(eng, FIXTURE_UNIVERSE_ID),
            eligibility_lookup=PITEligibilityLookup(eng, FIXTURE_UNIVERSE_ID),
            strategy_config_path=strategy_config_path,
        )
        out = capsys.readouterr().out
        assert "AAA" not in out and "GGG" not in out
        assert "HHH" in out and "III" in out and "JJJ" in out

    def test_permissive_threshold_keeps_members(self, eligibility_engine, tmp_path, capsys):
        from data.universe.runtime import PITEligibilityLookup, PITUniverseLookup

        eng, prices = eligibility_engine
        strategy_config_path = self._write_strategy_config(
            tmp_path, {"price_usd": {"op": "gte", "threshold": 0.01}}
        )
        start = sorted(prices["date"].unique())[273]
        end = prices["date"].max()
        mock_snaps = _mock_snapshots(prices)

        run(
            snapshot_date=date(2024, 1, 2),
            start=start,
            end=end,
            strategy_id="vtest",
            batch_size=20,
            dry_run=True,
            snapshots=mock_snaps,
            universe_id=FIXTURE_UNIVERSE_ID,
            universe_lookup=PITUniverseLookup(eng, FIXTURE_UNIVERSE_ID),
            eligibility_lookup=PITEligibilityLookup(eng, FIXTURE_UNIVERSE_ID),
            strategy_config_path=strategy_config_path,
        )
        out = capsys.readouterr().out
        assert "Would write" in out and "Would write 0 factor_score rows" not in out

    def test_unsupported_filter_fails_closed(self, eligibility_engine, tmp_path):
        """A strategy config declaring min_market_cap_usd (no PIT source,
        03A-4a's existing fail-closed contract) must still fail closed when
        reached through the scoring path, not just in a unit test of
        eligibility_config.py directly."""
        from data.universe.eligibility_config import UnsupportedEligibilityFilterError
        from data.universe.runtime import PITEligibilityLookup, PITUniverseLookup

        eng, prices = eligibility_engine
        path = tmp_path / "strategy.yaml"
        import yaml

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"universe": {"min_market_cap_usd": 500_000_000}}, f)

        start = sorted(prices["date"].unique())[273]
        end = prices["date"].max()
        mock_snaps = _mock_snapshots(prices)

        with pytest.raises(UnsupportedEligibilityFilterError):
            run(
                snapshot_date=date(2024, 1, 2),
                start=start,
                end=end,
                strategy_id="vtest",
                batch_size=20,
                dry_run=True,
                snapshots=mock_snaps,
                universe_id=FIXTURE_UNIVERSE_ID,
                universe_lookup=PITUniverseLookup(eng, FIXTURE_UNIVERSE_ID),
                eligibility_lookup=PITEligibilityLookup(eng, FIXTURE_UNIVERSE_ID),
                strategy_config_path=str(path),
            )

    def test_strategy_config_incompatible_with_provisional_no_universe(self, tmp_path):
        path = tmp_path / "strategy.yaml"
        import yaml

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"universe": {"eligibility": {"price_usd": {"op": "gte", "threshold": 1.0}}}}, f)

        with pytest.raises(ValueError, match="incompatible"):
            run(
                snapshot_date=date(2024, 1, 2),
                start=date(2021, 1, 4),
                end=date(2021, 6, 1),
                strategy_id="vtest",
                batch_size=20,
                dry_run=True,
                provisional_no_universe=True,
                strategy_config_path=str(path),
            )

    def test_no_eligibility_batch_data_fails_closed(self, tmp_path, monkeypatch):
        """Filters declared but the Phase B batch job never ran for this
        universe_id -- NoEligibilityDataError, not a silent all-pass.

        Exercises run()'s own DATABASE_URL-backed construction path (the
        eligibility_lookup=None default) rather than an injected lookup, so
        DATABASE_URL is monkeypatched to the local fixture DB for this test
        only -- never a real Postgres connection.
        """
        from data.universe.runtime import NoEligibilityDataError, PITUniverseLookup

        db_path = tmp_path / "no_eligibility_data.db"
        eng = create_engine(f"sqlite:///{db_path}", future=True)
        run_import(
            FixtureSP500Provider(),
            engine=eng,
            artifact_root=tmp_path / "artifacts",
            coverage_start=FIXTURE_COVERAGE_START,
        )
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        mock_snaps = _mock_snapshots(prices)
        strategy_config_path = self._write_strategy_config(
            tmp_path, {"price_usd": {"op": "gte", "threshold": 1.0}}
        )
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()

        with pytest.raises(NoEligibilityDataError):
            run(
                snapshot_date=date(2024, 1, 2),
                start=start,
                end=end,
                strategy_id="vtest",
                batch_size=20,
                dry_run=True,
                snapshots=mock_snaps,
                universe_id=FIXTURE_UNIVERSE_ID,
                universe_lookup=PITUniverseLookup(eng, FIXTURE_UNIVERSE_ID),
                strategy_config_path=strategy_config_path,
            )

    def test_requested_attribute_never_computed_fails_closed(self, tmp_path):
        """Codex P2 fix (PR #42 review, round 4): NoEligibilityDataError only
        catches "the batch job never ran at all for this universe_id" -- a
        universe_id with ONLY a security_type batch (no price_usd/
        adv_usd_20d rows at all) passes that check, but a strategy filtering
        on price_usd would previously have every ticker/date silently
        resolve to missing_attribute (indistinguishable from "everyone is
        illiquid") instead of failing closed with a clear "attribute was
        never computed" error."""
        from data.universe.eligibility_batch import write_security_type_batch
        from data.universe.runtime import PITEligibilityLookup, PITUniverseLookup

        eng = create_engine(f"sqlite:///{tmp_path / 'security_type_only.db'}", future=True)
        run_import(
            FixtureSP500Provider(),
            engine=eng,
            artifact_root=tmp_path / "artifacts",
            coverage_start=FIXTURE_COVERAGE_START,
        )
        # Only security_type has ever been computed for this universe_id.
        write_security_type_batch(eng, FIXTURE_UNIVERSE_ID, curation=[], code_version="test")

        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members, date(2021, 1, 4), 273 + 40)
        mock_snaps = _mock_snapshots(prices)
        strategy_config_path = self._write_strategy_config(
            tmp_path, {"price_usd": {"op": "gte", "threshold": 1.0}}
        )
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()

        with pytest.raises(ValueError, match="price_usd"):
            run(
                snapshot_date=date(2024, 1, 2),
                start=start,
                end=end,
                strategy_id="vtest",
                batch_size=20,
                dry_run=True,
                snapshots=mock_snaps,
                universe_id=FIXTURE_UNIVERSE_ID,
                universe_lookup=PITUniverseLookup(eng, FIXTURE_UNIVERSE_ID),
                eligibility_lookup=PITEligibilityLookup(eng, FIXTURE_UNIVERSE_ID),
                strategy_config_path=strategy_config_path,
            )


