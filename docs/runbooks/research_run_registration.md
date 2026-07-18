# Research run registration (required pre-deploy step for migration 012)

## Why this exists

BUG-009 section 4 (`docs/plans/01b-research-validity-design.md` §4) adds
versioned research identity: `research_methodologies` and `research_runs`,
with a `research_run_id` foreign key that is now part of the unique
constraint/primary key on `signal_ic_stats`, `factor_scores`, and
`alpha_scores` (Alembic migration `012_research_identity.py`). Every row
written to those tables after migration 012 must be tagged with an
**explicitly approved active research run** — the system never assumes the
newest row is the correct one to use (section 4 item 2).

`airflow/dags/daily_signal_pipeline.py` (the scheduled `30 21 * * 1-5`
factor/alpha scoring DAG) resolves its research run via a plain-SQL lookup
(`_get_active_research_run_id_sql`, semantically identical to
`data.research.identity.get_active_research_run` — see that function's
docstring for why the DAG cannot import the ORM version directly: the
packaged Airflow image pins SQLAlchemy 1.4.51, while `data.research.models`
uses SQLAlchemy-2-only APIs) and fails closed with a `RuntimeError` if none
is active. **The DAG never registers or activates a run itself** — that is
a deliberate operator action per section 4's design, not an oversight. This
registration script (`scripts/register_operational_research_run.py`) is a
standalone CLI run from the normal dev/ops Python environment (SQLAlchemy
2.x), not inside the packaged Airflow image, so it uses the ORM layer
directly — that asymmetry is intentional.

## Consequence if you skip this step

The first `daily_signal_pipeline` run scheduled *after* migration 012 has
been applied to a database will fail its `write_scores` task with:

```
RuntimeError: No active research run for methodology
'daily_signal_pipeline_operational' (BUG-009 section 4 / migration 012).
Run 'python -m scripts.register_operational_research_run' once ...
```

No factor/alpha scores are written until this is fixed. Paper trading and
downstream reporting that depend on fresh scores will stall.

## Required pre-deploy step

Run once, before or immediately after deploying migration 012 to a
database, and before the next scheduled `daily_signal_pipeline` run:

```powershell
python -m scripts.register_operational_research_run
```

This is **idempotent** — safe to run again. On first run it:

1. Registers a `research_methodologies` row named
   `daily_signal_pipeline_operational` describing the current 01B-3
   operational baseline (timing policy, corporate-action availability
   policies, missing-data policy, etc. — see
   `scripts/register_operational_research_run.py::_current_methodology_spec`).
2. Registers a `research_runs` row referencing that methodology and a
   `data_version` (defaults to today's date; pass `--data-version` to set
   one explicitly, e.g. matching a pinned snapshot).
3. Activates that run (`is_active = TRUE`, `status = 'active'`), which also
   deactivates any previously active run for the same methodology.

On a later run, if an active run already exists, it prints the existing
run's id and exits without creating anything (true no-op) — safe to include
in a deploy checklist that runs every time.

## Rolling to a new run (e.g. after a data refresh)

```powershell
python -m scripts.register_operational_research_run --force-new-run --data-version 2026-08-01
```

This registers and activates a NEW run, deactivating (never deleting) the
prior one. Old `factor_scores`/`alpha_scores` rows under the prior run
remain in the table, preserved, and are simply no longer the run any new
write targets — matching section 4's "preserve the old records" rule. This
command does **not** recompute any historical scores; it only changes which
run new writes are tagged with going forward.

## Verifying the fix

```powershell
python -c "
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from data.research.identity import get_active_research_run
import os
engine = create_engine(os.environ['DATABASE_URL'])
with Session(engine) as session:
    run = get_active_research_run(session, 'daily_signal_pipeline_operational')
    print(f'Active run: id={run.id} data_version={run.data_version} status={run.status}')
"
```

## Related

- `data/research/identity.py` — `register_methodology`, `register_run`,
  `activate_run`, `get_active_research_run`.
- `infra/db/migrations/versions/012_research_identity.py` — schema.
- `bugs.md` BUG-009 — full adversarial-review fix-round record, including
  this pre-merge/pre-deploy blocker note.
