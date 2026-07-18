"""Versioned research identity: register methodologies/runs and look up the
explicitly approved active run (BUG-009 section 4, design plan
docs/plans/01b-research-validity-design.md).

This module is deliberately small: it does not trigger any recompute of
historical scores/backtests. It only provides the identity/invalidation
machinery described in section 4 items 1-2: create methodology/run records,
mark one run active per methodology, and look it up explicitly rather than
assuming the newest row is valid.

Legacy backfill
-----------------
Migration 012 creates exactly one ``LEGACY_METHODOLOGY_NAME`` /
``LEGACY_RUN_LABEL`` pair at upgrade time and points every pre-existing
``signal_ic_stats`` / ``factor_scores`` / ``alpha_scores`` row at it via
``research_run_id``. That legacy run's ``status`` is ``'legacy_provisional'``
and ``is_active`` is ``False`` — it is preserved for traceability but is
never selected by :func:`get_active_research_run` (which raises
``NoActiveResearchRunError`` until an operator explicitly activates a new
run). This generalizes migration 010's ``signal_ic_stats.provisional``
boolean without removing it: ``provisional`` stays for backward
read-compatibility with anything still querying it directly, but the
authoritative status now lives on ``research_runs.status`` /
``research_runs.is_active`` via the ``research_run_id`` foreign key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from data.research.models import ResearchMethodology, ResearchRun

LEGACY_METHODOLOGY_NAME = "legacy_provisional_pre_01b3"
LEGACY_RUN_LABEL = "legacy_provisional_backfill"
# Distinct from signals.research.timing.DEFAULT_TIMING_POLICY.policy_id on
# purpose: this identifies the historical same-close bug (BUG-009), not the
# fixed baseline convention, so a reader can immediately tell legacy rows
# apart from anything computed after 01B-3.
LEGACY_TIMING_POLICY_ID = "legacy_same_close_v0"
LEGACY_POLICY_VALUE = "legacy_unknown"


class ResearchIdentityError(Exception):
    """Base class for research-identity failures."""


class NoActiveResearchRunError(ResearchIdentityError):
    """No run is marked active for the requested methodology."""


class MultipleActiveResearchRunsError(ResearchIdentityError):
    """More than one run is marked active — ambiguous, should be unreachable
    if callers only ever use :func:`activate_run` (which deactivates any
    prior active run for the same methodology first), but checked
    defensively rather than assumed."""


@dataclass(frozen=True)
class MethodologySpec:
    """Fields required to register a new :class:`ResearchMethodology`."""

    name: str
    universe_import_policy: str
    timing_policy_id: str
    score_action_availability_policy: str
    realized_return_action_availability_policy: str
    action_source_version: str
    return_adjustment_policy: str
    missing_data_policy: str
    code_config_hash: str
    universe_id: Optional[str] = None
    notes: Optional[str] = None


def register_methodology(session: Session, spec: MethodologySpec) -> ResearchMethodology:
    """Insert a new :class:`ResearchMethodology` row. Never updates an existing one
    in place — a changed policy is a NEW methodology, not a mutation (section 4)."""
    methodology = ResearchMethodology(
        name=spec.name,
        universe_id=spec.universe_id,
        universe_import_policy=spec.universe_import_policy,
        timing_policy_id=spec.timing_policy_id,
        score_action_availability_policy=spec.score_action_availability_policy,
        realized_return_action_availability_policy=spec.realized_return_action_availability_policy,
        action_source_version=spec.action_source_version,
        return_adjustment_policy=spec.return_adjustment_policy,
        missing_data_policy=spec.missing_data_policy,
        code_config_hash=spec.code_config_hash,
        notes=spec.notes,
        created_at=datetime.now(tz=timezone.utc),
    )
    session.add(methodology)
    session.flush()
    return methodology


def register_run(
    session: Session,
    methodology_id: int,
    data_version: str,
    *,
    run_label: Optional[str] = None,
    status: str = "candidate",
    notes: Optional[str] = None,
) -> ResearchRun:
    """Insert a new :class:`ResearchRun`. Not active until :func:`activate_run`."""
    if not data_version:
        raise ValueError(
            "data_version is required (C7: every research run must record the "
            "data snapshot version)."
        )
    run = ResearchRun(
        methodology_id=methodology_id,
        data_version=data_version,
        run_label=run_label,
        status=status,
        is_active=False,
        created_at=datetime.now(tz=timezone.utc),
        notes=notes,
    )
    session.add(run)
    session.flush()
    return run


def activate_run(session: Session, run_id: int, activated_by: str) -> ResearchRun:
    """Mark `run_id` as THE active run for its methodology.

    Deactivates any other run currently active for the same methodology
    first, so at most one run per methodology is ever active — the
    "explicitly approved active run" that :func:`get_active_research_run`
    selects, never the newest row (section 4 item 2).
    """
    run = session.get(ResearchRun, run_id)
    if run is None:
        raise ResearchIdentityError(f"research_runs.id={run_id} does not exist")

    session.execute(
        update(ResearchRun)
        .where(ResearchRun.methodology_id == run.methodology_id, ResearchRun.is_active.is_(True))
        .values(is_active=False)
    )
    run.is_active = True
    run.status = "active"
    run.activated_at = datetime.now(tz=timezone.utc)
    run.activated_by = activated_by
    session.flush()
    return run


def get_active_research_run(session: Session, methodology_name: str) -> ResearchRun:
    """Look up THE explicitly approved active run for `methodology_name`.

    Never falls back to "the newest row" (section 4 item 2). Raises
    :class:`NoActiveResearchRunError` if no run has been activated, or
    :class:`MultipleActiveResearchRunsError` if more than one is
    (should be unreachable given :func:`activate_run`'s deactivation step,
    but checked rather than assumed).
    """
    methodology = session.execute(
        select(ResearchMethodology).where(ResearchMethodology.name == methodology_name)
    ).scalar_one_or_none()
    if methodology is None:
        raise ResearchIdentityError(f"No research_methodologies row named {methodology_name!r}")

    active_runs = session.execute(
        select(ResearchRun).where(
            ResearchRun.methodology_id == methodology.id,
            ResearchRun.is_active.is_(True),
        )
    ).scalars().all()

    if not active_runs:
        raise NoActiveResearchRunError(
            f"No active research run for methodology {methodology_name!r}. "
            "Call activate_run() to explicitly approve one before querying "
            "operational/current scores (section 4 item 2)."
        )
    if len(active_runs) > 1:
        raise MultipleActiveResearchRunsError(
            f"{len(active_runs)} active runs found for methodology "
            f"{methodology_name!r}; expected exactly one."
        )
    return active_runs[0]


def get_legacy_run_id(engine: Engine) -> int:
    """Return the id of the migrated legacy run (migration 012 backfill).

    Convenience for callers that need to distinguish legacy_provisional rows
    from anything computed after 01B-3 without re-deriving the name/label.
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT rr.id FROM research_runs rr "
                "JOIN research_methodologies rm ON rm.id = rr.methodology_id "
                "WHERE rm.name = :name AND rr.run_label = :label"
            ),
            {"name": LEGACY_METHODOLOGY_NAME, "label": LEGACY_RUN_LABEL},
        ).fetchone()
    if row is None:
        raise ResearchIdentityError(
            "No legacy research run found. Has migration 012 been applied?"
        )
    return int(row[0])
