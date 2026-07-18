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
from data.universe.runtime import CoverageGapError, PITUniverseLookup
from scripts.backfill_momentum_scores import run


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


class TestBackfillPITFilter:
    def test_non_member_scores_are_filtered_out(self, lookup, capsys) -> None:
        # 273+ lookback days before the score window; XXX is priced but never
        # a member of the fixture universe.
        members = ["AAA", "GGG", "HHH", "III", "JJJ"]
        prices = _make_prices(members + ["XXX"], date(2021, 1, 4), 273 + 40)
        start = sorted(prices["date"].dt.date.unique())[273]
        end = prices["date"].dt.date.max()

        mock_snaps = MagicMock()
        mock_snaps.load_snapshot.return_value = prices

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

        mock_snaps = MagicMock()
        mock_snaps.load_snapshot.return_value = prices

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
