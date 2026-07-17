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
