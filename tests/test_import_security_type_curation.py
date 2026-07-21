"""Tests for scripts/import_security_type_curation.py (03A-4b, Phase B of
BUG-078).

Covers the script's own logic -- curation-file YAML parsing and the
dry-run/live wiring -- separately from data/tests/universe/test_eligibility_batch.py's
coverage of the underlying data.universe.eligibility_batch module functions
(adversarial-review P2, PR #42: CLI scripts previously had no dedicated
tests of their own).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine

from data.universe.eligibility_batch import EmptyBatchError, SecurityTypeCurationEntry
from data.universe.import_pipeline import run_import
from data.universe.providers.fixture_provider import (
    FIXTURE_COVERAGE_START,
    FIXTURE_UNIVERSE_ID,
    FixtureSP500Provider,
)
from scripts.import_security_type_curation import load_curation_file, run


class TestLoadCurationFile:
    def test_empty_seed_file_parses_to_zero_entries_default_cs(self, tmp_path):
        path = tmp_path / "curation.yaml"
        path.write_text("default_security_type: CS\nentries: []\n", encoding="utf-8")
        default_security_type, entries = load_curation_file(str(path))
        assert default_security_type == "CS"
        assert entries == []

    def test_parses_entry_with_effective_end_and_note(self, tmp_path):
        path = tmp_path / "curation.yaml"
        path.write_text(
            "default_security_type: CS\n"
            "entries:\n"
            "  - ticker: AAA\n"
            "    security_type: REIT\n"
            "    effective_start: '2020-06-01'\n"
            "    effective_end: '2021-01-01'\n"
            "    note: 'test source'\n",
            encoding="utf-8",
        )
        default_security_type, entries = load_curation_file(str(path))
        assert len(entries) == 1
        entry = entries[0]
        assert isinstance(entry, SecurityTypeCurationEntry)
        assert entry.ticker == "AAA"
        assert entry.security_type == "REIT"
        assert entry.effective_start == date(2020, 6, 1)
        assert entry.effective_end == date(2021, 1, 1)
        assert entry.note == "test source"

    def test_accepts_unquoted_yaml_date_scalars(self, tmp_path):
        """Codex P2 fix (PR #42 second review, round 2): PyYAML's default
        SafeLoader resolves an UNQUOTED YYYY-MM-DD scalar natively to
        datetime.date (YAML 1.1 timestamp resolution) -- the seed file's own
        documented schema shows this exact unquoted form, so it must be
        accepted, not raise TypeError before any curation can be imported."""
        path = tmp_path / "curation.yaml"
        path.write_text(
            "entries:\n"
            "  - ticker: AAA\n"
            "    security_type: REIT\n"
            "    effective_start: 2020-06-01\n"  # unquoted -- parses as date
            "    effective_end: 2021-01-01\n"  # unquoted -- parses as date
            "    note: test source\n",
            encoding="utf-8",
        )
        default_security_type, entries = load_curation_file(str(path))
        assert len(entries) == 1
        entry = entries[0]
        assert entry.effective_start == date(2020, 6, 1)
        assert entry.effective_end == date(2021, 1, 1)

    def test_parses_entry_with_no_effective_end_as_open_ended(self, tmp_path):
        path = tmp_path / "curation.yaml"
        path.write_text(
            "entries:\n"
            "  - ticker: AAA\n"
            "    security_type: ADR\n"
            "    effective_start: '2020-01-01'\n",
            encoding="utf-8",
        )
        _, entries = load_curation_file(str(path))
        assert entries[0].effective_end is None
        assert entries[0].note == ""

    def test_missing_default_security_type_falls_back_to_cs(self, tmp_path):
        path = tmp_path / "curation.yaml"
        path.write_text("entries: []\n", encoding="utf-8")
        default_security_type, _ = load_curation_file(str(path))
        assert default_security_type == "CS"

    def test_empty_file_treated_as_no_entries(self, tmp_path):
        path = tmp_path / "curation.yaml"
        path.write_text("", encoding="utf-8")
        default_security_type, entries = load_curation_file(str(path))
        assert default_security_type == "CS"
        assert entries == []


@pytest.fixture
def published_universe_engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'curation_run.db'}", future=True)
    run_import(
        FixtureSP500Provider(),
        engine=eng,
        artifact_root=tmp_path / "artifacts",
        coverage_start=FIXTURE_COVERAGE_START,
    )
    return eng


class TestRunDryRunVsLive:
    def test_dry_run_prints_summary_without_writing(self, published_universe_engine, tmp_path, capsys):
        path = tmp_path / "curation.yaml"
        path.write_text("entries: []\n", encoding="utf-8")
        run(
            universe_id=FIXTURE_UNIVERSE_ID,
            curation_file=str(path),
            dry_run=True,
            engine=published_universe_engine,
        )
        out = capsys.readouterr().out
        assert "[DRY RUN] Would write" in out
        assert "0 hand-curated tickers" in out

    def test_live_run_writes_and_prints_batch_id(self, published_universe_engine, tmp_path, capsys):
        path = tmp_path / "curation.yaml"
        path.write_text("entries: []\n", encoding="utf-8")
        run(
            universe_id=FIXTURE_UNIVERSE_ID,
            curation_file=str(path),
            dry_run=False,
            code_version="test",
            engine=published_universe_engine,
        )
        out = capsys.readouterr().out
        assert "Wrote batch_id=" in out

    def test_dry_run_fails_closed_when_no_published_membership(self, tmp_path):
        """Codex-review-adjacent P2 fix: a dry-run against a universe_id with
        no published membership must raise, not silently report success."""
        eng = create_engine(f"sqlite:///{tmp_path / 'no_membership.db'}", future=True)
        path = tmp_path / "curation.yaml"
        path.write_text("entries: []\n", encoding="utf-8")
        with pytest.raises(EmptyBatchError):
            run(
                universe_id="never_imported",
                curation_file=str(path),
                dry_run=True,
                engine=eng,
            )

    def test_live_run_fails_closed_when_no_published_membership(self, tmp_path):
        eng = create_engine(f"sqlite:///{tmp_path / 'no_membership_live.db'}", future=True)
        path = tmp_path / "curation.yaml"
        path.write_text("entries: []\n", encoding="utf-8")
        with pytest.raises(EmptyBatchError):
            run(
                universe_id="never_imported",
                curation_file=str(path),
                dry_run=False,
                code_version="test",
                engine=eng,
            )

    def test_curated_entry_reflected_in_dry_run_summary(self, published_universe_engine, tmp_path, capsys):
        path = tmp_path / "curation.yaml"
        path.write_text(
            "entries:\n"
            "  - ticker: AAA\n"
            "    security_type: ADR\n"
            "    effective_start: '2020-01-01'\n",
            encoding="utf-8",
        )
        run(
            universe_id=FIXTURE_UNIVERSE_ID,
            curation_file=str(path),
            dry_run=True,
            engine=published_universe_engine,
        )
        out = capsys.readouterr().out
        assert "1 hand-curated tickers" in out
