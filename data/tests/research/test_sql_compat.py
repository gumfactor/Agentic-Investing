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
