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


# ─── Fix round: header validation + snapshot tamper detection ─────────────────


class TestChangesTableHeaderValidation:
    """Adversarial-review fix: the changes-table parser must validate the
    ACTUAL header text before its positional rename, so a silently reordered
    or renamed Wikipedia table fails closed instead of mis-assigning every
    column."""

    def _multiindex_df(self, top_pairs):
        import pandas as pd

        cols = pd.MultiIndex.from_tuples(top_pairs)
        return pd.DataFrame(
            [["June 30, 2026", "AAA", "Alpha", "BBB", "Bravo", "Reason text"]],
            columns=cols,
        )

    def test_expected_headers_parse(self) -> None:
        df = self._multiindex_df([
            ("Effective Date", "Effective Date"),
            ("Added", "Ticker"),
            ("Added", "Security"),
            ("Removed", "Ticker"),
            ("Removed", "Security"),
            ("Reason", "Reason"),
        ])
        events = WikipediaSP500Provider._parse_changes_table(df)
        assert len(events) == 1
        assert events[0].added_ticker == "AAA"
        assert events[0].removed_ticker == "BBB"

    def test_reordered_headers_fail_closed(self) -> None:
        # Added and Removed groups swapped: positional renaming would have
        # silently inverted every membership event.
        df = self._multiindex_df([
            ("Effective Date", "Effective Date"),
            ("Removed", "Ticker"),
            ("Removed", "Security"),
            ("Added", "Ticker"),
            ("Added", "Security"),
            ("Reason", "Reason"),
        ])
        with pytest.raises(ValueError, match="reordered"):
            WikipediaSP500Provider._parse_changes_table(df)

    def test_renamed_header_fails_closed(self) -> None:
        df = self._multiindex_df([
            ("Announcement Date", "Announcement Date"),
            ("Added", "Ticker"),
            ("Added", "Security"),
            ("Removed", "Ticker"),
            ("Removed", "Security"),
            ("Reason", "Reason"),
        ])
        with pytest.raises(ValueError, match="headers changed"):
            WikipediaSP500Provider._parse_changes_table(df)

    def test_unexpected_flat_headers_fail_closed(self) -> None:
        import pandas as pd

        df = pd.DataFrame(
            [["June 30, 2026", "AAA", "BBB", "extra"]],
            columns=["Effective Date", "Added Ticker", "Removed Ticker", "Bonus Column"],
        )
        with pytest.raises(ValueError, match="unexpected flat"):
            WikipediaSP500Provider._parse_changes_table(df)


class TestSnapshotTamperDetection:
    """Adversarial-review fix: exercise the checksum-mismatch path the
    snapshot loader docstring claims (fetch() must refuse a modified
    artifact)."""

    def test_tampered_snapshot_rejected(self, tmp_path: Path) -> None:
        import shutil

        day_dir = tmp_path / "snap"
        day_dir.mkdir()
        shutil.copy(_SNAPSHOT, day_dir / "raw.html")
        shutil.copy(_SNAPSHOT.parent / "manifest.json", day_dir / "manifest.json")

        # Corrupt one byte of the copied artifact.
        raw = (day_dir / "raw.html").read_bytes()
        (day_dir / "raw.html").write_bytes(raw[:100] + b"X" + raw[101:])

        provider = WikipediaSP500Provider(snapshot_path=day_dir / "raw.html")
        with pytest.raises(ValueError, match="checksum mismatch"):
            provider.fetch()

    def test_untampered_snapshot_accepted(self, tmp_path: Path) -> None:
        import shutil

        day_dir = tmp_path / "snap"
        day_dir.mkdir()
        shutil.copy(_SNAPSHOT, day_dir / "raw.html")
        shutil.copy(_SNAPSHOT.parent / "manifest.json", day_dir / "manifest.json")

        provider = WikipediaSP500Provider(snapshot_path=day_dir / "raw.html")
        raw = provider.fetch()
        assert len(raw.content) > 0
