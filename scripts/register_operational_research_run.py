"""Register and activate the operational research run daily_signal_pipeline.py needs.

BUG-009 section 4 / migration 012 pre-deploy blocker (adversarial-review P1
finding, 01B-3): once migration 012 lands, ``factor_scores``/``alpha_scores``
require a ``research_run_id`` on every row, and
``airflow/dags/daily_signal_pipeline.py::_write_scores`` resolves it via
``data.research.identity.get_active_research_run(session,
"daily_signal_pipeline_operational")`` — which raises
``NoActiveResearchRunError`` until an operator has explicitly registered and
activated a run for that methodology name. This DAG never registers or
activates one itself (section 4: "never assume the newest row"), so without
running this script once, the next scheduled DAG run
(``30 21 * * 1-5``) fails closed.

This script is idempotent — safe to run again:

- If no ``research_methodologies`` row named
  ``daily_signal_pipeline_operational`` exists, it registers one describing
  the CURRENT operational baseline (the t+1 close timing policy, the
  conservative-next-session corporate-action availability policy, etc.).
- If that methodology exists but has no active run, it registers a new run
  and activates it.
- If an active run already exists, it prints the existing run's details and
  exits 0 without creating anything (true no-op).
- ``--force-new-run`` registers and activates a NEW run even if one is
  already active (e.g. after a data snapshot refresh) — the prior run is
  deactivated, not deleted, per section 4's "preserve the old records" rule.

Usage:
    python -m scripts.register_operational_research_run
    python -m scripts.register_operational_research_run --data-version 2026-07-18
    python -m scripts.register_operational_research_run --force-new-run --data-version 2026-08-01

Does NOT trigger any recompute of historical scores — this is identity/
invalidation machinery only (design plan section 4 implementation order).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from data.research.identity import (
    MethodologySpec,
    NoActiveResearchRunError,
    activate_run,
    get_active_research_run,
    register_methodology,
    register_run,
)
from data.research.models import ResearchMethodology
from signals.research.timing import DEFAULT_TIMING_POLICY

load_dotenv()

_OPERATIONAL_METHODOLOGY_NAME = "daily_signal_pipeline_operational"


def _current_methodology_spec() -> MethodologySpec:
    """The methodology description matching the current 01B-3 operational
    baseline. Update this (and register a NEW methodology row — never edit
    an existing one in place) if any of these policies change."""
    return MethodologySpec(
        name=_OPERATIONAL_METHODOLOGY_NAME,
        universe_id="sp500",
        universe_import_policy="pit_universe_effective_dated_v1",
        timing_policy_id=DEFAULT_TIMING_POLICY.policy_id,
        score_action_availability_policy="score_cutoff_known_at_v1",
        realized_return_action_availability_policy="exit_cutoff_known_at_v1",
        # Matches actual current behavior (adversarial review round 7):
        # data/ingestion/market/yfinance_client.py and
        # airflow/dags/daily_data_pipeline.py both call
        # TimescaleWriter.upsert_corporate_actions(df) without a
        # source_version argument, so every row actually lands with the
        # writer's own default, "unknown" -- not a real yfinance version
        # string. Registering "yfinance-current" here would have been a
        # provenance claim ingestion doesn't back up, undermining exactly
        # what migrations 011/012 were built to guarantee. See the notes
        # field below for the tightening path.
        action_source_version="unknown",
        return_adjustment_policy="total_return_adjusted_v1",
        missing_data_policy="pct_change_fill_none_v1",
        code_config_hash="01b3-daily-signal-pipeline-baseline",
        notes=(
            "Operational baseline for airflow/dags/daily_signal_pipeline.py. "
            "Registered by scripts/register_operational_research_run.py. "
            "action_source_version is 'unknown' because ingestion "
            "(data/ingestion/market/yfinance_client.py, "
            "airflow/dags/daily_data_pipeline.py) does not currently pass a "
            "real source_version to TimescaleWriter.upsert_corporate_actions "
            "-- tighten this (and re-register a NEW methodology, never edit "
            "this one in place) once ingestion is updated to pass one, e.g. "
            "an actual yfinance library version string."
        ),
    )


def ensure_operational_run(
    session: Session,
    data_version: str,
    force_new_run: bool,
    activated_by: str,
) -> tuple[int, str, int, bool]:
    """Idempotent core: returns (methodology_id, methodology_name, run_id,
    created_new_run).

    Deliberately returns plain scalars, not the ORM ``ResearchMethodology``
    instance (adversarial-review round 8 P1 finding). ``session.commit()``
    below expires the ORM object's attributes by default
    (``expire_on_commit=True``), and the caller reads the return value AFTER
    the ``with Session(...)`` block in ``main()`` has closed the session --
    any attribute access on a still-attached-but-expired instance at that
    point re-queries via the now-closed session and raises
    ``DetachedInstanceError``. Reading the scalars here, while the session is
    still open, avoids that lifecycle hazard entirely rather than papering
    over it with ``expire_on_commit=False`` or an explicit refresh.
    """
    methodology = session.query(ResearchMethodology).filter_by(
        name=_OPERATIONAL_METHODOLOGY_NAME
    ).one_or_none()
    if methodology is None:
        methodology = register_methodology(session, _current_methodology_spec())
        session.commit()
    methodology_id = methodology.id
    methodology_name = methodology.name

    if not force_new_run:
        try:
            active_run = get_active_research_run(session, _OPERATIONAL_METHODOLOGY_NAME)
            return methodology_id, methodology_name, active_run.id, False
        except NoActiveResearchRunError:
            pass  # fall through to register + activate below

    run = register_run(
        session,
        methodology_id,
        data_version=data_version,
        run_label=f"operational_{data_version}",
        notes="Registered by scripts/register_operational_research_run.py.",
    )
    session.commit()
    run_id = run.id
    activate_run(session, run_id, activated_by=activated_by)
    session.commit()
    return methodology_id, methodology_name, run_id, True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-version",
        default=None,
        help="Data snapshot version tag for a newly-created run (C7). "
        "Defaults to today's date if a new run must be created.",
    )
    parser.add_argument(
        "--force-new-run",
        action="store_true",
        help="Register and activate a NEW run even if one is already active "
        "(the prior run is deactivated, not deleted).",
    )
    parser.add_argument(
        "--activated-by",
        default=os.environ.get("USER") or os.environ.get("USERNAME") or "operator",
        help="Identity recorded on the activated run.",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 1

    data_version = args.data_version or date.today().isoformat()

    engine = create_engine(database_url)
    with Session(engine) as session:
        methodology_id, methodology_name, run_id, created = ensure_operational_run(
            session, data_version, args.force_new_run, args.activated_by
        )

    if created:
        print(
            f"Registered and activated research_runs.id={run_id} for methodology "
            f"{methodology_name!r} (id={methodology_id}), "
            f"data_version={data_version!r}."
        )
    else:
        print(
            f"Active run already exists: research_runs.id={run_id} for methodology "
            f"{methodology_name!r} (id={methodology_id}). No-op. "
            "Use --force-new-run to activate a fresh run instead."
        )
    print("daily_signal_pipeline.py is now unblocked to write factor_scores/alpha_scores.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
