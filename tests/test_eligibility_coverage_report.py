"""Tests for scripts/eligibility_coverage_report.py (03A-4b, Phase B of
BUG-078).

Covers the script's own logic -- date sampling and report-formatting/gap
tallying (including the Codex P2 NaN/None gap-tally fix) -- separately from
data/tests/universe/test_eligibility_batch.py's coverage of the underlying
data.universe.eligibility_batch.eligibility_coverage_report function
(adversarial-review, PR #42: CLI scripts previously had no dedicated tests
of their own).
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import create_engine

from data.universe.eligibility_batch import write_price_eligibility_batch
from data.universe.import_pipeline import run_import
from data.universe.providers.fixture_provider import (
    FIXTURE_COVERAGE_START,
    FIXTURE_UNIVERSE_ID,
    FixtureSP500Provider,
)
from scripts.eligibility_coverage_report import _dates_in_range, run


class TestDatesInRange:
    def test_step_one_includes_every_day(self):
        dates = _dates_in_range(date(2024, 1, 1), date(2024, 1, 5), 1)
        assert dates == [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]

    def test_step_n_samples_every_nth_day(self):
        dates = _dates_in_range(date(2024, 1, 1), date(2024, 1, 10), 3)
        assert dates == [date(2024, 1, 1), date(2024, 1, 4), date(2024, 1, 7), date(2024, 1, 10)]

    def test_single_day_range(self):
        assert _dates_in_range(date(2024, 1, 1), date(2024, 1, 1), 1) == [date(2024, 1, 1)]


@pytest.fixture
def published_universe_engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'coverage_run.db'}", future=True)
    run_import(
        FixtureSP500Provider(),
        engine=eng,
        artifact_root=tmp_path / "artifacts",
        coverage_start=FIXTURE_COVERAGE_START,
    )
    return eng


class TestRunReportFormatting:
    def test_summary_json_includes_expected_fields(self, published_universe_engine, tmp_path):
        output_path = tmp_path / "report.json"
        summary = run(
            universe_id=FIXTURE_UNIVERSE_ID,
            start=date(2020, 1, 2),
            end=date(2020, 1, 8),
            sample_every_n_days=1,
            output=str(output_path),
            engine=published_universe_engine,
        )
        assert summary["universe_id"] == FIXTURE_UNIVERSE_ID
        assert "market_cap_usd" in summary["excluded_attributes"]
        assert output_path.exists()
        with open(output_path, encoding="utf-8") as f:
            written = json.load(f)
        assert written == summary

    def test_no_gap_when_all_dates_out_of_coverage(self, published_universe_engine, capsys):
        """Codex P2 fix (PR #42 review): a range entirely BEFORE the
        published membership's coverage_start must report zero gap rows
        (every row is out-of-coverage and excluded from the tally), not a
        spurious gap count from NaN/None round-tripping through
        DataFrame.to_dict()/JSON."""
        summary = run(
            universe_id=FIXTURE_UNIVERSE_ID,
            start=date(1999, 1, 1),
            end=date(1999, 1, 5),
            sample_every_n_days=1,
            engine=published_universe_engine,
        )
        assert summary["n_rows_with_gaps"] == 0
        assert all(not r["in_coverage"] for r in summary["by_date"])
        out = capsys.readouterr().out
        assert "0 (date, attribute) rows" in out

    def test_gap_count_correct_with_mixed_in_and_out_of_coverage_dates(
        self, published_universe_engine
    ):
        """Mixes an out-of-coverage date (before FIXTURE_COVERAGE_START) with
        in-coverage dates that have a real gap (no eligibility rows written
        at all), proving the fix counts only genuine in-coverage gaps."""
        summary = run(
            universe_id=FIXTURE_UNIVERSE_ID,
            start=date(2019, 12, 30),  # out of coverage (before FIXTURE_COVERAGE_START)
            end=date(2020, 1, 3),  # in coverage, no attribute rows written -> real gaps
            sample_every_n_days=1,
            engine=published_universe_engine,
        )
        out_of_coverage_rows = [r for r in summary["by_date"] if not r["in_coverage"]]
        in_coverage_rows = [r for r in summary["by_date"] if r["in_coverage"]]
        assert out_of_coverage_rows  # sanity: the mix actually includes both
        assert in_coverage_rows
        # Every out-of-coverage row must be excluded from the gap tally
        # regardless of how many in-coverage gap rows exist.
        real_gaps = sum(1 for r in in_coverage_rows if r["n_missing"])
        assert summary["n_rows_with_gaps"] == real_gaps

    def test_security_type_curated_and_default_counts_surfaced(self, published_universe_engine):
        summary = run(
            universe_id=FIXTURE_UNIVERSE_ID,
            start=date(2020, 1, 2),
            end=date(2020, 1, 2),
            sample_every_n_days=1,
            engine=published_universe_engine,
        )
        assert summary["n_security_type_curated_tickers"] == 0
        assert summary["n_security_type_default_tickers"] == 0  # no security_type batch run yet

    def test_gap_closes_once_price_attributes_populated(self, published_universe_engine):
        dates = [date(2020, 1, 2), date(2020, 1, 3)]
        prices_rows = []
        for t in ["AAA", "GGG", "HHH", "III", "JJJ", "DDD", "EEE"]:
            for d in dates:
                prices_rows.append({"ticker": t, "date": d, "close": 100.0, "volume": 1_000_000})
        import pandas as pd

        write_price_eligibility_batch(
            published_universe_engine,
            FIXTURE_UNIVERSE_ID,
            pd.DataFrame(prices_rows),
            start=dates[0],
            end=dates[-1],
            code_version="test",
        )
        summary = run(
            universe_id=FIXTURE_UNIVERSE_ID,
            start=dates[0],
            end=dates[-1],
            sample_every_n_days=1,
            engine=published_universe_engine,
        )
        price_rows = [r for r in summary["by_date"] if r["attribute_name"] == "price_usd"]
        assert all(r["n_missing"] == 0 for r in price_rows)
        assert summary["n_rows_with_gaps"] < len(summary["by_date"])
