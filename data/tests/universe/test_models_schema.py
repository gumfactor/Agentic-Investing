"""Schema sanity tests for data/universe/models.py (BUG-008, 01B-2 Phase 1).

These exercise the ORM models directly against SQLite (no live Postgres
required), mirroring the pattern used by strategy_registry's tests. The
Postgres-only EXCLUDE-overlap constraint from the Alembic migration is not
present on SQLite; overlap rejection at that layer is covered separately by
the import pipeline's Python-level validation (Phase 2).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.universe.models import Base, SymbolHistory, UniverseImportBatch, UniverseMembership


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path / 'universe_test.db'}", future=True)
    Base.metadata.create_all(eng)
    return eng


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class TestUniverseImportBatch:
    def test_insert_and_read_back(self, engine) -> None:
        with Session(engine) as session:
            batch = UniverseImportBatch(
                universe_id="sp500",
                provider="fixture_sp500",
                source_version="fixture-v1",
                raw_artifact_path="data/vendor/sp500_fixture/raw.html",
                raw_checksum_sha256="a" * 64,
                retrieved_at=_now(),
                status="published",
                coverage_start=date(2020, 1, 1),
                coverage_end=date(2024, 12, 31),
                n_membership_rows=10,
                n_symbol_history_rows=1,
                created_at=_now(),
                published_at=_now(),
            )
            session.add(batch)
            session.commit()
            session.refresh(batch)
            assert batch.id is not None
            assert batch.status == "published"

    def test_invalid_status_rejected(self, engine) -> None:
        with Session(engine) as session:
            batch = UniverseImportBatch(
                universe_id="sp500",
                provider="fixture_sp500",
                source_version="fixture-v1",
                raw_artifact_path="x",
                raw_checksum_sha256="a" * 64,
                retrieved_at=_now(),
                status="bogus_status",
                created_at=_now(),
            )
            session.add(batch)
            with pytest.raises(IntegrityError):
                session.commit()


class TestUniverseMembership:
    def _make_row(self, **overrides) -> UniverseMembership:
        defaults = dict(
            universe_id="sp500",
            ticker="AAPL",
            vendor_symbol=None,
            effective_start=date(2020, 1, 1),
            effective_end=None,
            source="fixture_sp500",
            source_record_id="row-1",
            announced_at=None,
            known_at=_now(),
            source_version="fixture-v1",
            ingested_at=_now(),
            reason=None,
        )
        defaults.update(overrides)
        return UniverseMembership(**defaults)

    def test_insert_open_interval(self, engine) -> None:
        with Session(engine) as session:
            session.add(self._make_row())
            session.commit()

        with Session(engine) as session:
            row = session.query(UniverseMembership).one()
            assert row.effective_end is None
            assert row.ticker == "AAPL"

    def test_effective_end_before_start_rejected(self, engine) -> None:
        with Session(engine) as session:
            session.add(
                self._make_row(
                    effective_start=date(2020, 6, 1), effective_end=date(2020, 1, 1)
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()

    def test_effective_end_equal_start_rejected(self, engine) -> None:
        # Half-open interval must not be empty.
        with Session(engine) as session:
            session.add(
                self._make_row(
                    effective_start=date(2020, 6, 1), effective_end=date(2020, 6, 1)
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()

    def test_empty_universe_id_rejected(self, engine) -> None:
        with Session(engine) as session:
            session.add(self._make_row(universe_id=""))
            with pytest.raises(IntegrityError):
                session.commit()

    def test_known_at_required(self, engine) -> None:
        with Session(engine) as session:
            session.add(
                UniverseMembership(
                    universe_id="sp500",
                    ticker="AAPL",
                    effective_start=date(2020, 1, 1),
                    source="fixture_sp500",
                    source_record_id="row-1",
                    source_version="fixture-v1",
                    ingested_at=_now(),
                    # known_at intentionally omitted
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()


class TestSymbolHistory:
    def test_insert_rename(self, engine) -> None:
        with Session(engine) as session:
            session.add(
                SymbolHistory(
                    universe_id="sp500",
                    old_ticker="SATS",
                    new_ticker="ECHO",
                    effective_date=date(2026, 6, 24),
                    source="fixture_sp500",
                    source_record_id="rename-1",
                    known_at=_now(),
                    source_version="fixture-v1",
                    ingested_at=_now(),
                    reason="ticker symbol change",
                )
            )
            session.commit()

        with Session(engine) as session:
            row = session.query(SymbolHistory).one()
            assert (row.old_ticker, row.new_ticker) == ("SATS", "ECHO")

    def test_same_old_and_new_ticker_rejected(self, engine) -> None:
        with Session(engine) as session:
            session.add(
                SymbolHistory(
                    universe_id="sp500",
                    old_ticker="AAPL",
                    new_ticker="AAPL",
                    effective_date=date(2026, 6, 24),
                    source="fixture_sp500",
                    source_record_id="rename-bad",
                    known_at=_now(),
                    source_version="fixture-v1",
                    ingested_at=_now(),
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()

    def test_duplicate_change_rejected(self, engine) -> None:
        with Session(engine) as session:
            session.add(
                SymbolHistory(
                    universe_id="sp500",
                    old_ticker="SATS",
                    new_ticker="ECHO",
                    effective_date=date(2026, 6, 24),
                    source="fixture_sp500",
                    source_record_id="rename-1",
                    known_at=_now(),
                    source_version="fixture-v1",
                    ingested_at=_now(),
                )
            )
            session.commit()

        with Session(engine) as session:
            session.add(
                SymbolHistory(
                    universe_id="sp500",
                    old_ticker="SATS",
                    new_ticker="ECHO",
                    effective_date=date(2026, 6, 24),
                    source="fixture_sp500",
                    source_record_id="rename-1-dup",
                    known_at=_now(),
                    source_version="fixture-v1",
                    ingested_at=_now(),
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
