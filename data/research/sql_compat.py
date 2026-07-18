"""SQLAlchemy-1.4-compatible plain-SQL lookups for research identity.

BUG-009 section 4. ``data.research.identity``/``data.research.models`` use
SQLAlchemy-2-only APIs (``DeclarativeBase``/``Mapped``/``mapped_column``),
but the packaged Airflow runtime image pins SQLAlchemy 1.4.51 (see
``infra/docker/Dockerfile.airflow``) and any code path reachable from an
Airflow DAG task — including scripts imported *by* a DAG task, not just the
DAG module itself — must not import them, even lazily inside a function:
the import only fails when that code path actually *executes* inside the
packaged image, not at DAG parse time, so a lazy import doesn't make it
safe, only quieter.

This module exists so the same plain-``text()`` active-run lookup does not
get reimplemented (or, worse, silently regress to the ORM import) at every
call site that turns out to be Airflow-reachable. Round 2 of the 01B-3 PR's
adversarial review found this exact bug in
``airflow/dags/daily_signal_pipeline.py::_write_scores`` (fixed by writing
a local plain-SQL lookup there); round 5 found the identical bug in
``scripts/paper_inputs_check.py`` (Airflow-reachable via
``airflow/dags/daily_paper_trading.py``'s ``_verify_inputs``/
``_construct_target``, which import ``paper_inputs_check.run``/
``CheckRecorder``/``load_strategy_config``) — reached this time because a
later fix round added a NEW active-run filter there without reusing this
pattern. Centralizing it here is meant to stop this recurring a third time.

Kept in semantic lockstep with
``data.research.identity.get_active_research_run`` — see that function's
docstring for the ORM-based equivalent used everywhere that is NOT
Airflow-reachable (standalone CLI tools such as
``scripts/register_operational_research_run.py``, which run in the normal
SQLAlchemy 2.x dev/ops environment, not the packaged Airflow image).
"""

from __future__ import annotations

from typing import Union


def get_active_research_run_id(engine_or_url: Union[object, str], methodology_name: str) -> int:
    """Resolve the id of the explicitly active ``research_runs`` row for
    ``methodology_name`` via plain SQL (SQLAlchemy Core only — no ORM).

    Raises ``RuntimeError`` with an actionable message if no run is active
    (mirrors ``NoActiveResearchRunError``) or if more than one is (mirrors
    ``MultipleActiveResearchRunsError`` — should be unreachable given the
    partial unique index in migration 012, checked rather than assumed).
    Never assumes the newest row is correct (section 4 item 2): only a row
    with ``is_active = TRUE`` is returned.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(engine_or_url) if isinstance(engine_or_url, str) else engine_or_url

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rr.id FROM research_runs rr "
                "JOIN research_methodologies rm ON rm.id = rr.methodology_id "
                "WHERE rm.name = :name AND rr.is_active = TRUE"
            ),
            {"name": methodology_name},
        ).fetchall()

    if not rows:
        raise RuntimeError(
            f"No active research run for methodology {methodology_name!r} "
            "(BUG-009 section 4 / migration 012). Run "
            "'python -m scripts.register_operational_research_run' once (see "
            "docs/runbooks/research_run_registration.md) before this code "
            "path can read research-run-scoped rows."
        )
    if len(rows) > 1:
        raise RuntimeError(
            f"{len(rows)} active research runs found for methodology "
            f"{methodology_name!r}; expected exactly one. This should be "
            "prevented by migration 012's partial unique index — investigate "
            "before trusting any research-run-scoped read."
        )
    return int(rows[0][0])
