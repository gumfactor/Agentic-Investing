"""Tests for scripts/backfill_eligibility_attributes.py (03A-4b, Phase B of
BUG-078).

Covers the script's own logic -- lookback-window SQL construction and the
dry-run/live wiring -- separately from data/tests/universe/test_eligibility_batch.py's
coverage of the underlying data.universe.eligibility_batch module functions
(adversarial-review P2, PR #42: CLI scripts previously had no dedicated
tests of their own).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from data.universe.eligibility_batch import EmptyBatchError
from data.universe.calendar import is_trading_session
from scripts.backfill_eligibility_attributes import _load_prices_for_range, run


def _trading_dates(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if is_trading_session(d):
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


@pytest.fixture
def daily_prices_engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'daily_prices.db'}", future=True)
    md = sa.MetaData()
    daily_prices = sa.Table(
        "daily_prices",
        md,
        sa.Column("ticker", sa.String(20)),
        sa.Column("date", sa.Date()),
        sa.Column("close", sa.Numeric(18, 6)),
        sa.Column("volume", sa.BigInteger()),
    )
    md.create_all(eng)

    dates = _trading_dates(date(2020, 1, 2), 40)
    rows = []
    for t in ["AAA", "BBB"]:
        level = 100.0
        for d in dates:
            level *= 1.001
            rows.append({"ticker": t, "date": d, "close": round(level, 4), "volume": 1_000_000})
    with eng.begin() as conn:
        conn.execute(daily_prices.insert(), rows)
    return eng, dates


class TestLoadPricesForRange:
    def test_includes_enough_trailing_sessions_for_adv_window(self, daily_prices_engine):
        eng, dates = daily_prices_engine
        start = dates[25]
        end = dates[-1]
        prices = _load_prices_for_range(eng, start, end, adv_window=20)
        loaded_dates = sorted(pd.to_datetime(prices["date"]).dt.date.unique())
        # 19 trailing sessions before `start` needed for a full 20-session window.
        assert loaded_dates[0] == dates[25 - 19]
        assert loaded_dates[-1] == end

    def test_adv_window_le_1_needs_no_lookback(self, daily_prices_engine):
        """Codex-adjacent P3 fix: adv_window<=1 must not fetch the entire
        prior history -- lookback_start should be exactly `start`."""
        eng, dates = daily_prices_engine
        start = dates[25]
        end = dates[-1]
        prices = _load_prices_for_range(eng, start, end, adv_window=1)
        loaded_dates = sorted(pd.to_datetime(prices["date"]).dt.date.unique())
        assert loaded_dates[0] == start

    def test_short_history_falls_back_to_earliest_available_date(self, daily_prices_engine):
        eng, dates = daily_prices_engine
        start = dates[5]  # fewer than adv_window-1=19 trailing sessions exist
        end = dates[-1]
        prices = _load_prices_for_range(eng, start, end, adv_window=20)
        loaded_dates = sorted(pd.to_datetime(prices["date"]).dt.date.unique())
        assert loaded_dates[0] == dates[0]

    def test_no_prior_history_uses_start_itself(self, daily_prices_engine):
        eng, dates = daily_prices_engine
        start = dates[0]
        end = dates[-1]
        prices = _load_prices_for_range(eng, start, end, adv_window=20)
        loaded_dates = sorted(pd.to_datetime(prices["date"]).dt.date.unique())
        assert loaded_dates[0] == start


class TestRunDryRunVsLive:
    def test_dry_run_prints_summary_without_writing(self, daily_prices_engine, capsys):
        eng, dates = daily_prices_engine
        run(
            universe_id="sp500_test",
            start=dates[25],
            end=dates[-1],
            adv_window=20,
            dry_run=True,
            engine=eng,
        )
        out = capsys.readouterr().out
        assert "[DRY RUN] Would write" in out
        assert "price_usd" in out and "adv_usd_20d" in out

    def test_live_run_writes_and_prints_batch_id(self, daily_prices_engine, capsys):
        eng, dates = daily_prices_engine
        run(
            universe_id="sp500_test",
            start=dates[25],
            end=dates[-1],
            adv_window=20,
            dry_run=False,
            code_version="test",
            engine=eng,
        )
        out = capsys.readouterr().out
        assert "Wrote batch_id=" in out

    def test_dry_run_fails_closed_on_empty_result_like_a_live_run_would(
        self, daily_prices_engine
    ):
        """Codex-review-adjacent P2 fix: a dry-run must raise the same
        EmptyBatchError a live run would for the same (empty) input, not
        silently report '0 rows, success'."""
        eng, dates = daily_prices_engine
        empty_prices = pd.DataFrame(columns=["ticker", "date", "close", "volume"])
        with pytest.raises(EmptyBatchError):
            run(
                universe_id="sp500_test",
                start=dates[25],
                end=dates[-1],
                adv_window=20,
                dry_run=True,
                engine=eng,
                prices=empty_prices,
            )

    def test_live_run_fails_closed_on_empty_result(self, daily_prices_engine):
        eng, dates = daily_prices_engine
        empty_prices = pd.DataFrame(columns=["ticker", "date", "close", "volume"])
        with pytest.raises(EmptyBatchError):
            run(
                universe_id="sp500_test",
                start=dates[25],
                end=dates[-1],
                adv_window=20,
                dry_run=False,
                code_version="test",
                engine=eng,
                prices=empty_prices,
            )
