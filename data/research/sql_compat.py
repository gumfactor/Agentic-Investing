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

Round 11 added a second shared function here for the same reason:
:func:`assert_methodology_write_is_honest` is the ONE enforcement point
every score-writing call site (the DAG's ``_write_scores``/
``_write_simulation``, ``scripts/backfill_momentum_scores.py``,
``scripts/validate_signal_ic.py``'s persist path) must call before
persisting, instead of each maintaining its own ad hoc
"does this write's actual state match what its methodology claims" check
— see that function's own docstring for the full history of why this
consolidation happened (four separate near-duplicate variants across
rounds 9-11 before this).

Kept in semantic lockstep with
``data.research.identity.get_active_research_run`` — see that function's
docstring for the ORM-based equivalent used everywhere that is NOT
Airflow-reachable (standalone CLI tools such as
``scripts/register_operational_research_run.py``, which run in the normal
SQLAlchemy 2.x dev/ops environment, not the packaged Airflow image).
"""

from __future__ import annotations

from typing import Union


class MethodologyHonestyError(RuntimeError):
    """Raised by :func:`assert_methodology_write_is_honest` when a caller's
    actual write state does not match what the run's registered
    methodology claims (BUG-009 section 4)."""


def _resolve_methodology_fields(engine_or_url: Union[object, str], research_run_id: int) -> dict:
    """Plain-SQL lookup of the (name, universe_import_policy,
    score_action_availability_policy) of the methodology a research_run_id
    is tagged with. No ORM -- safe for Airflow-reachable code."""
    from sqlalchemy import create_engine, text

    engine = create_engine(engine_or_url) if isinstance(engine_or_url, str) else engine_or_url

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT rm.name, rm.universe_import_policy, "
                "rm.score_action_availability_policy "
                "FROM research_runs rr "
                "JOIN research_methodologies rm ON rm.id = rr.methodology_id "
                "WHERE rr.id = :run_id"
            ),
            {"run_id": research_run_id},
        ).mappings().fetchone()

    if row is None:
        raise ValueError(
            f"research_run_id={research_run_id} does not exist in research_runs "
            "(or its methodology_id does not exist in research_methodologies)."
        )
    return dict(row)


def assert_methodology_write_is_honest(
    engine_or_url: Union[object, str],
    research_run_id: int,
    *,
    pit_universe_applied: bool,
    corporate_action_adjustment_applied: bool,
) -> None:
    """Single, shared enforcement point for BUG-009 section 4's "a write
    must not lie about its own methodology" invariant.

    Adversarial-review round 11: this is the FOURTH time this exact defect
    class has surfaced --

    1. The original P0 finding (missing research_run_id wiring altogether).
    2. Round 8's ``register_operational_research_run.py`` DetachedInstanceError
       (different bug, same subsystem).
    3. Round 9/10's per-script honesty checks --
       ``scripts/backfill_momentum_scores.py``'s
       ``_validate_raw_prices_methodology_is_honest`` (corporate-action
       dimension) and ``_validate_provisional_no_universe_methodology_is_honest``
       (universe dimension), then a second, near-identical copy of the
       latter added to ``scripts/validate_signal_ic.py``.
    4. Round 11: ``airflow/dags/daily_signal_pipeline.py``'s own degrade
       path (no honesty check had ever been added to the DAG itself,
       since the ad hoc checks were only ever built into
       standalone scripts).

    Rather than adding a fifth near-duplicate variant, every score-writing
    call site in the repo (the DAG's ``_write_scores``, the DAG's
    ``_write_simulation`` fallback tagging, ``backfill_momentum_scores.py``,
    ``validate_signal_ic.py``'s persist path) now routes through this ONE
    function instead. It takes the caller's ACTUAL state as explicit
    keyword booleans -- "did I really apply PIT-universe filtering?", "did
    I really apply corporate-action cutoff adjustment?" -- rather than
    trying to infer intent from a specific CLI flag name (which is what
    made each prior variant script-specific and prone to being
    reimplemented instead of reused). Plain SQL only (no ORM), matching
    :func:`get_active_research_run_id` above, so this is safe to call from
    Airflow-reachable code as well as standalone scripts -- there is no
    longer a reason for Airflow-reachable and non-Airflow-reachable call
    sites to diverge onto separate honesty-check implementations.

    Args:
        research_run_id: the run a write is about to be tagged with.
        pit_universe_applied: True iff point-in-time universe membership
            filtering was actually applied to the rows about to be
            written (BUG-008). Raises if the run's methodology claims PIT
            safety (``universe_import_policy ==
            "pit_universe_effective_dated_v1"``) but this is False.
        corporate_action_adjustment_applied: True iff cutoff-aware
            corporate-action price adjustment was actually applied to the
            rows about to be written (BUG-009 section 2.3). Raises if the
            run's methodology claims cutoff-adjustment
            (``score_action_availability_policy ==
            "score_cutoff_known_at_v1"``) but this is False.

    Raises:
        ValueError: research_run_id does not resolve to a methodology.
        MethodologyHonestyError: the methodology's claim does not match
            the caller's actual state.
    """
    fields = _resolve_methodology_fields(engine_or_url, research_run_id)
    name = fields["name"]

    if fields["universe_import_policy"] == "pit_universe_effective_dated_v1" and not pit_universe_applied:
        raise MethodologyHonestyError(
            f"research_run_id={research_run_id} is tagged with methodology "
            f"{name!r}, whose universe_import_policy is "
            "'pit_universe_effective_dated_v1' -- it claims point-in-time "
            "universe membership filtering was applied. This write did NOT "
            "actually apply PIT filtering (degraded/provisional/current-"
            "membership scores). Persisting under that methodology would "
            "misrepresent what was actually computed (BUG-008/BUG-009). "
            "Either ensure PIT filtering actually succeeds before writing, "
            "or register a distinct methodology whose universe_import_policy "
            "honestly declares no PIT filtering was applied and tag the "
            "write with a run under that methodology instead."
        )

    if (
        fields["score_action_availability_policy"] == "score_cutoff_known_at_v1"
        and not corporate_action_adjustment_applied
    ):
        raise MethodologyHonestyError(
            f"research_run_id={research_run_id} is tagged with methodology "
            f"{name!r}, whose score_action_availability_policy is "
            "'score_cutoff_known_at_v1' -- it claims cutoff-adjusted "
            "corporate-action handling was applied. This write did NOT "
            "actually apply that adjustment (raw/unadjusted prices). "
            "Persisting under that methodology would misrepresent what was "
            "actually computed (BUG-009). Either ensure corporate-action "
            "adjustment actually succeeds before writing, or register a "
            "distinct methodology whose score_action_availability_policy "
            "honestly declares no cutoff adjustment was applied and tag the "
            "write with a run under that methodology instead."
        )


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
