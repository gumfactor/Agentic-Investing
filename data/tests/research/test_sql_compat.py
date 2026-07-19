"""Tests for data/research/sql_compat.py (BUG-009 section 4, round 5).

Plain-SQL (SQLAlchemy Core only, no ORM) active-run lookup shared by every
Airflow-reachable call site, so the SQLAlchemy-1.4-incompatible ORM import
cannot silently regress back in at a new call site.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from data.research.identity import MethodologySpec, activate_run, register_methodology, register_run
from data.research.models import Base
from data.research.sql_compat import get_active_research_run_id


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path / 'sql_compat_test.db'}", future=True)
    Base.metadata.create_all(eng)
    return eng


def _spec(name: str) -> MethodologySpec:
    return MethodologySpec(
        name=name,
        universe_import_policy="test",
        timing_policy_id="t_plus_1_close_v1",
        score_action_availability_policy="test",
        realized_return_action_availability_policy="test",
        action_source_version="test",
        return_adjustment_policy="test",
        missing_data_policy="test",
        code_config_hash="test",
    )


class TestGetActiveResearchRunId:
    def test_returns_active_run_id(self, engine) -> None:
        with Session(engine) as session:
            methodology = register_methodology(session, _spec("m1"))
            run = register_run(session, methodology.id, data_version="v1")
            session.commit()
            activate_run(session, run.id, activated_by="test")
            session.commit()
            expected_id = run.id

        result = get_active_research_run_id(engine, "m1")
        assert result == expected_id

    def test_raises_when_no_active_run(self, engine) -> None:
        with Session(engine) as session:
            register_methodology(session, _spec("m1"))
            session.commit()

        with pytest.raises(RuntimeError, match="No active research run"):
            get_active_research_run_id(engine, "m1")

    def test_raises_when_methodology_unknown(self, engine) -> None:
        with pytest.raises(RuntimeError, match="No active research run"):
            get_active_research_run_id(engine, "does_not_exist")

    def test_accepts_url_string(self, tmp_path: Path) -> None:
        db_path = tmp_path / "url_test.db"
        url = f"sqlite:///{db_path}"
        eng = create_engine(url, future=True)
        Base.metadata.create_all(eng)
        with Session(eng) as session:
            methodology = register_methodology(session, _spec("m1"))
            run = register_run(session, methodology.id, data_version="v1")
            session.commit()
            activate_run(session, run.id, activated_by="test")
            session.commit()
            expected_id = run.id

        result = get_active_research_run_id(url, "m1")
        assert result == expected_id

    def test_matches_orm_get_active_research_run(self, engine) -> None:
        from data.research.identity import get_active_research_run

        with Session(engine) as session:
            methodology = register_methodology(session, _spec("m1"))
            run = register_run(session, methodology.id, data_version="v1")
            session.commit()
            activate_run(session, run.id, activated_by="test")
            session.commit()

        sql_result = get_active_research_run_id(engine, "m1")
        with Session(engine) as session:
            orm_result = get_active_research_run(session, "m1")
        assert sql_result == orm_result.id


class TestAssertMethodologyWriteIsHonest:
    """BUG-009 section 4, adversarial-review round 11: the ONE shared
    enforcement point every score-writing call site routes through, instead
    of each maintaining its own ad hoc honesty check (four separate
    near-duplicate variants existed across rounds 9-11 before this
    consolidation)."""

    def _register(self, engine, *, universe_import_policy, score_action_availability_policy, name):
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
            run = register_run(session, methodology.id, data_version="2026-01-01")
            session.commit()
            activate_run(session, run.id, activated_by="test")
            session.commit()
            return run.id

    def test_pit_claiming_run_raises_when_pit_not_applied(self, engine) -> None:
        from data.research.sql_compat import (
            MethodologyHonestyError,
            assert_methodology_write_is_honest,
        )

        run_id = self._register(
            engine,
            universe_import_policy="pit_universe_effective_dated_v1",
            score_action_availability_policy="raw_unadjusted_no_corporate_action_data",
            name="claims_pit",
        )

        with pytest.raises(MethodologyHonestyError, match="misrepresent"):
            assert_methodology_write_is_honest(
                engine,
                run_id,
                pit_universe_applied=False,
                corporate_action_adjustment_applied=True,
            )

    def test_pit_claiming_run_passes_when_pit_applied(self, engine) -> None:
        from data.research.sql_compat import assert_methodology_write_is_honest

        run_id = self._register(
            engine,
            universe_import_policy="pit_universe_effective_dated_v1",
            score_action_availability_policy="raw_unadjusted_no_corporate_action_data",
            name="claims_pit_and_true",
        )

        assert_methodology_write_is_honest(
            engine,
            run_id,
            pit_universe_applied=True,
            corporate_action_adjustment_applied=True,
        )  # must not raise

    def test_cutoff_claiming_run_raises_when_adjustment_not_applied(self, engine) -> None:
        from data.research.sql_compat import (
            MethodologyHonestyError,
            assert_methodology_write_is_honest,
        )

        run_id = self._register(
            engine,
            universe_import_policy="legacy_current_membership_no_pit_enforcement",
            score_action_availability_policy="score_cutoff_known_at_v1",
            name="claims_cutoff",
        )

        with pytest.raises(MethodologyHonestyError, match="misrepresent"):
            assert_methodology_write_is_honest(
                engine,
                run_id,
                pit_universe_applied=False,
                corporate_action_adjustment_applied=False,
            )

    def test_cutoff_claiming_run_passes_when_adjustment_applied(self, engine) -> None:
        from data.research.sql_compat import assert_methodology_write_is_honest

        run_id = self._register(
            engine,
            universe_import_policy="legacy_current_membership_no_pit_enforcement",
            score_action_availability_policy="score_cutoff_known_at_v1",
            name="claims_cutoff_and_true",
        )

        assert_methodology_write_is_honest(
            engine,
            run_id,
            pit_universe_applied=False,
            corporate_action_adjustment_applied=True,
        )  # must not raise

    def test_honest_legacy_methodology_never_raises(self, engine) -> None:
        """A methodology that claims neither PIT nor cutoff-adjustment can
        never be dishonest on either dimension, regardless of what the
        caller actually did."""
        from data.research.sql_compat import assert_methodology_write_is_honest

        run_id = self._register(
            engine,
            universe_import_policy="legacy_current_membership_no_pit_enforcement",
            score_action_availability_policy="raw_unadjusted_no_corporate_action_data",
            name="fully_honest_legacy",
        )

        assert_methodology_write_is_honest(
            engine, run_id, pit_universe_applied=False, corporate_action_adjustment_applied=False
        )  # must not raise
        assert_methodology_write_is_honest(
            engine, run_id, pit_universe_applied=True, corporate_action_adjustment_applied=True
        )  # also fine -- doing MORE than claimed is never dishonest

    def test_raises_on_unknown_run_id(self, engine) -> None:
        from data.research.sql_compat import assert_methodology_write_is_honest

        with pytest.raises(ValueError, match="does not exist"):
            assert_methodology_write_is_honest(
                engine, 999999, pit_universe_applied=True, corporate_action_adjustment_applied=True
            )

    def test_accepts_url_string_like_get_active_research_run_id(self, tmp_path) -> None:
        """Same engine_or_url flexibility as get_active_research_run_id."""
        from data.research.sql_compat import assert_methodology_write_is_honest

        db_path = tmp_path / "url_string.db"
        url = f"sqlite:///{db_path}"
        eng = create_engine(url, future=True)
        Base.metadata.create_all(eng)
        run_id = self._register(
            eng,
            universe_import_policy="legacy_current_membership_no_pit_enforcement",
            score_action_availability_policy="raw_unadjusted_no_corporate_action_data",
            name="url_string_case",
        )

        assert_methodology_write_is_honest(
            url, run_id, pit_universe_applied=False, corporate_action_adjustment_applied=False
        )  # must not raise


class TestSqlCompatHasNoOrmImports:
    """The whole point of this module: it must never import the
    SQLAlchemy-2-only ORM (data.research.identity/data.research.models),
    or every Airflow-reachable caller that delegates to it inherits the
    incompatibility right back."""

    def test_no_banned_imports(self) -> None:
        import ast

        module_path = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "data" / "research" / "sql_compat.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        banned = ("data.research.identity", "data.research.models")
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(banned):
                    offenders.append(node.module)
            elif isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.startswith(banned)]
        assert offenders == []
