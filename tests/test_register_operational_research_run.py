"""Tests for scripts/register_operational_research_run.py.

BUG-009 section 4 P1 adversarial-review fix: daily_signal_pipeline.py would
hard-fail its first post-migration-012 run without an active research run
for methodology 'daily_signal_pipeline_operational'. This script registers
and activates one idempotently.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from data.research.identity import get_active_research_run
from data.research.models import Base
from scripts.register_operational_research_run import (
    _OPERATIONAL_METHODOLOGY_NAME,
    ensure_operational_run,
)


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path / 'register_run_test.db'}", future=True)
    Base.metadata.create_all(eng)
    return eng


class TestEnsureOperationalRun:
    def test_first_call_creates_methodology_and_activates_run(self, engine) -> None:
        with Session(engine) as session:
            methodology, run_id, created = ensure_operational_run(
                session, data_version="2026-07-18", force_new_run=False, activated_by="op1"
            )
            assert created is True
            assert methodology.name == _OPERATIONAL_METHODOLOGY_NAME

            active = get_active_research_run(session, _OPERATIONAL_METHODOLOGY_NAME)
            assert active.id == run_id
            assert active.is_active is True
            assert active.activated_by == "op1"

    def test_second_call_is_a_true_no_op(self, engine) -> None:
        with Session(engine) as session:
            _m1, run_id_1, created_1 = ensure_operational_run(
                session, data_version="2026-07-18", force_new_run=False, activated_by="op1"
            )
            assert created_1 is True

        with Session(engine) as session:
            _m2, run_id_2, created_2 = ensure_operational_run(
                session, data_version="2026-07-19", force_new_run=False, activated_by="op2"
            )
            assert created_2 is False
            # Same run id — no new row, no reactivation, no-op confirmed.
            assert run_id_2 == run_id_1

    def test_force_new_run_deactivates_prior_and_activates_new(self, engine) -> None:
        with Session(engine) as session:
            _m1, run_id_1, _ = ensure_operational_run(
                session, data_version="2026-07-18", force_new_run=False, activated_by="op1"
            )

        with Session(engine) as session:
            _m2, run_id_2, created_2 = ensure_operational_run(
                session, data_version="2026-08-01", force_new_run=True, activated_by="op2"
            )
            assert created_2 is True
            assert run_id_2 != run_id_1

            active = get_active_research_run(session, _OPERATIONAL_METHODOLOGY_NAME)
            assert active.id == run_id_2
            assert active.data_version == "2026-08-01"

        # Prior run preserved, just no longer active (section 4: "preserve
        # the old records", never overwrite/delete).
        from data.research.models import ResearchRun

        with Session(engine) as session:
            prior = session.get(ResearchRun, run_id_1)
            assert prior is not None
            assert prior.is_active is False

    def test_daily_signal_pipeline_can_resolve_the_registered_run(self, engine) -> None:
        """End-to-end proof this actually unblocks the DAG's own lookup call."""
        with Session(engine) as session:
            ensure_operational_run(
                session, data_version="2026-07-18", force_new_run=False, activated_by="op1"
            )

        with Session(engine) as session:
            # Exactly what airflow/dags/daily_signal_pipeline.py::_write_scores calls.
            run = get_active_research_run(session, "daily_signal_pipeline_operational")
            assert run.id is not None
