"""Shared test helper: register+activate a research run for the operational
methodology on an in-memory/sqlite test engine (BUG-009 section 4).

Every paper-trading preflight script reads alpha_scores/factor_scores
filtered to the explicitly active research_run_id for
"daily_signal_pipeline_operational" (scripts/paper_inputs_check.py
::_resolve_active_research_run_id). Test fixtures across
tests/test_paper_inputs_check.py, tests/test_paper_target_check.py,
tests/test_paper_order_candidates_check.py,
tests/test_paper_risk_compliance_check.py, and
tests/test_paper_stage_blotter_check.py all need to register one before
writing rows to a fake alpha_scores table, or every one of those scripts'
`run()` calls fails closed with "No active research run" (correctly, per the
production fix) rather than exercising the behavior each test is actually
about. Centralized here instead of copy-pasted per file.
"""

from __future__ import annotations

_OPERATIONAL_METHODOLOGY_NAME = "daily_signal_pipeline_operational"


def setup_active_research_run(engine, *, methodology_name: str = _OPERATIONAL_METHODOLOGY_NAME) -> int:
    """Create research_methodologies/research_runs on `engine` and return the
    id of a freshly activated run for `methodology_name`."""
    from sqlalchemy.orm import Session

    from data.research.identity import MethodologySpec, activate_run, register_methodology, register_run
    from data.research.models import Base

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        methodology = register_methodology(
            session,
            MethodologySpec(
                name=methodology_name,
                universe_import_policy="test",
                timing_policy_id="t_plus_1_close_v1",
                score_action_availability_policy="test",
                realized_return_action_availability_policy="test",
                action_source_version="test",
                return_adjustment_policy="test",
                missing_data_policy="test",
                code_config_hash="test",
            ),
        )
        run = register_run(session, methodology.id, data_version="test")
        session.commit()
        activate_run(session, run.id, activated_by="test")
        session.commit()
        return run.id
