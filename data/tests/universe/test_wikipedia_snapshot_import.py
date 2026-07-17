"""Offline tests for the Wikipedia provider against the checked-in raw snapshot.

No network I/O: these parse ``data/vendor/wikipedia_sp500/2026-07-17/raw.html``
(the checksummed artifact saved by the 2026-07-17 verification import — see
docs/plans/01b2-constituent-source-contract.md "Import verification"). They
pin the real-data reconstruction results so a parser regression or artifact
tampering is caught in CI.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from data.universe.import_pipeline import coverage_report, run_import
from data.universe.providers.wikipedia_sp500 import WikipediaSP500Provider

_SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "vendor"
    / "wikipedia_sp500"
    / "2026-07-17"
    / "raw.html"
)
# Documented ticker-collision exclusions — see the source-contract doc.
_EXCLUSIONS = {"AGN", "AN", "SUN"}

pytestmark = pytest.mark.skipif(
    not _SNAPSHOT.exists(), reason="checked-in Wikipedia snapshot not present"
)


@pytest.fixture(scope="module")
def parsed():
    provider = WikipediaSP500Provider(snapshot_path=_SNAPSHOT)
    raw = provider.fetch()
    return provider.parse(raw)


class TestSnapshotParse:
    def test_current_rows_count(self, parsed) -> None:
        assert len(parsed.current_rows) == 503

    def test_change_events_count(self, parsed) -> None:
        assert len(parsed.change_events) == 407

    def test_universe_id_is_sp500(self, parsed) -> None:
        assert parsed.universe_id == "sp500"

    def test_known_rename_present(self, parsed) -> None:
        # 2026-06-24: EchoStar changed its ticker symbol from SATS to ECHO.
        renames = [
            e
            for e in parsed.change_events
            if e.removed_ticker == "SATS" and e.added_ticker == "ECHO"
        ]
        assert len(renames) == 1
        assert renames[0].effective_date == date(2026, 6, 24)

    def test_dot_tickers_normalized_to_dash(self, parsed) -> None:
        tickers = {r.ticker for r in parsed.current_rows}
        assert "BRK-B" in tickers
        assert "BRK.B" not in tickers


@pytest.mark.slow
class TestSnapshotFullImport:
    def test_import_publishes_with_documented_exclusions(self, tmp_path: Path) -> None:
        engine = create_engine(f"sqlite:///{tmp_path / 'wiki.db'}", future=True)
        provider = WikipediaSP500Provider(snapshot_path=_SNAPSHOT)
        batch = run_import(
            provider,
            engine=engine,
            artifact_root=tmp_path / "artifacts",
            coverage_start=date(1976, 7, 1),
            exclude_tickers=_EXCLUSIONS,
        )
        assert batch.status == "published"
        assert batch.universe_id == "sp500"
        assert batch.n_membership_rows == 890
        assert batch.n_symbol_history_rows == 6
        assert batch.coverage_start == date(1976, 7, 1)
        assert batch.coverage_end == date(2026, 7, 17)

        report = coverage_report(
            engine, "sp500", dates=[date(2010, 1, 4), date(2023, 6, 1)]
        )
        by_date = report.by_date.set_index("date")
        assert by_date.loc[date(2010, 1, 4), "n_members"] == 502
        assert by_date.loc[date(2023, 6, 1), "n_members"] == 519
