"""Tests for data/research/identity.py and data/research/models.py.

BUG-009 section 4 (versioned research identity / invalidation). Exercises
the ORM models directly against SQLite, mirroring
data/tests/universe/test_models_schema.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from data.research.identity import (
    MethodologySpec,
    MultipleActiveResearchRunsError,
    NoActiveResearchRunError,
    ResearchIdentityError,
    activate_run,
    get_active_research_run,
    register_methodology,
    register_run,
)
from data.research.models import Base, ResearchMethodology, ResearchRun


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path / 'research_identity_test.db'}", future=True)
    Base.metadata.create_all(eng)
    return eng


def _spec(name: str = "test_methodology_v1") -> MethodologySpec:
    return MethodologySpec(
        name=name,
        universe_import_policy="sp500_import_batch_7",
        timing_policy_id="t_plus_1_close_v1",
        score_action_availability_policy="score_cutoff_known_at_v1",
        realized_return_action_availability_policy="exit_cutoff_known_at_v1",
        action_source_version="yfinance-0.2.40",
        return_adjustment_policy="total_return_adjusted_v1",
        missing_data_policy="pct_change_fill_none_v1",
        code_config_hash="abc123",
    )


class TestRegisterMethodologyAndRun:
    def test_register_methodology_inserts_row(self, engine) -> None:
        with Session(engine) as session:
            methodology = register_methodology(session, _spec())
            session.commit()
            assert methodology.id is not None
            fetched = session.get(ResearchMethodology, methodology.id)
            assert fetched.name == "test_methodology_v1"
            assert fetched.timing_policy_id == "t_plus_1_close_v1"

    def test_duplicate_methodology_name_rejected(self, engine) -> None:
        with Session(engine) as session:
            register_methodology(session, _spec())
            session.commit()
        with Session(engine) as session:
            with pytest.raises(Exception):
                register_methodology(session, _spec())
                session.commit()

    def test_register_run_requires_data_version(self, engine) -> None:
        with Session(engine) as session:
            methodology = register_methodology(session, _spec())
            session.commit()
            with pytest.raises(ValueError, match="data_version"):
                register_run(session, methodology.id, data_version="")

    def test_register_run_not_active_by_default(self, engine) -> None:
        with Session(engine) as session:
            methodology = register_methodology(session, _spec())
            run = register_run(session, methodology.id, data_version="snapshot_2026_07_01")
            session.commit()
            assert run.is_active is False
            assert run.status == "candidate"


class TestActivateRunAndActiveLookup:
    def test_no_active_run_raises(self, engine) -> None:
        with Session(engine) as session:
            register_methodology(session, _spec())
            session.commit()
            with pytest.raises(NoActiveResearchRunError):
                get_active_research_run(session, "test_methodology_v1")

    def test_activated_run_is_returned(self, engine) -> None:
        with Session(engine) as session:
            methodology = register_methodology(session, _spec())
            run = register_run(session, methodology.id, data_version="snapshot_2026_07_01")
            session.commit()
            activate_run(session, run.id, activated_by="operator1")
            session.commit()

            active = get_active_research_run(session, "test_methodology_v1")
            assert active.id == run.id
            assert active.is_active is True
            assert active.status == "active"
            assert active.activated_by == "operator1"

    def test_activating_new_run_deactivates_prior_run(self, engine) -> None:
        """A new run cannot leave two runs simultaneously active — the newest
        activation always wins, and get_active_research_run never has to
        guess which of several active rows is authoritative."""
        with Session(engine) as session:
            methodology = register_methodology(session, _spec())
            run1 = register_run(session, methodology.id, data_version="v1")
            run2 = register_run(session, methodology.id, data_version="v2")
            session.commit()

            activate_run(session, run1.id, activated_by="op")
            session.commit()
            activate_run(session, run2.id, activated_by="op")
            session.commit()

            active = get_active_research_run(session, "test_methodology_v1")
            assert active.id == run2.id

            stale = session.get(ResearchRun, run1.id)
            assert stale.is_active is False

    def test_active_lookup_never_assumes_newest_row(self, engine) -> None:
        """Registering a NEWER run without activating it must not change
        which run get_active_research_run returns."""
        with Session(engine) as session:
            methodology = register_methodology(session, _spec())
            run1 = register_run(session, methodology.id, data_version="v1")
            session.commit()
            activate_run(session, run1.id, activated_by="op")
            session.commit()

            # A newer candidate run exists but was never activated.
            register_run(session, methodology.id, data_version="v2")
            session.commit()

            active = get_active_research_run(session, "test_methodology_v1")
            assert active.id == run1.id

    def test_unknown_methodology_raises(self, engine) -> None:
        with Session(engine) as session:
            with pytest.raises(ResearchIdentityError):
                get_active_research_run(session, "does_not_exist")


class TestNewRunCannotOverwriteOldMethodology:
    """Section 4 item 1: a new run cannot silently overwrite an old
    methodology's rows. Two methodologies must be able to hold rows for the
    SAME (ticker, score_date, factor_name, strategy_id) tuple simultaneously
    once research_run_id is part of the identity — proven at the
    factor_scores/alpha_scores/signal_ic_stats schema layer by migration 012;
    here we prove the run/methodology layer supports two independent runs
    that never collide with each other's identity."""

    def test_two_methodologies_can_each_have_their_own_active_run(self, engine) -> None:
        with Session(engine) as session:
            m1 = register_methodology(session, _spec("methodology_a"))
            m2 = register_methodology(session, _spec("methodology_b"))
            r1 = register_run(session, m1.id, data_version="v1")
            r2 = register_run(session, m2.id, data_version="v1")
            session.commit()
            activate_run(session, r1.id, activated_by="op")
            activate_run(session, r2.id, activated_by="op")
            session.commit()

            active_a = get_active_research_run(session, "methodology_a")
            active_b = get_active_research_run(session, "methodology_b")
            assert active_a.id != active_b.id
            assert active_a.methodology_id == m1.id
            assert active_b.methodology_id == m2.id
