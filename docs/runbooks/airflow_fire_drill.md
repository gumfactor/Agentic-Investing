# Airflow Recovery Fire Drill

Validates that the daily pipeline recovers cleanly when Airflow is interrupted mid-run.
Run this before promoting to a live trading environment.

> **Scope note:** this drill exercises crash recovery of `daily_signal_pipeline`
> under `LocalExecutor`. It is a separate exercise from Gate 01A (making the
> Compose paper runtime executable -- BUG-001 through BUG-004; see
> `docs/plans/01a-compose-paper-runtime-checklist.md`). Passing Gate 01A does
> not imply this drill has been run, and vice versa; run both before any live
> trading go-live decision.

---

## Prerequisites

- Docker stack running (`make up`)
- At least one successful scheduled or manual DAG run already in history
- TimescaleDB populated (`daily_prices` has rows)

---

## Procedure

### 1. Trigger a manual run

Open the Airflow UI at `http://localhost:8080`.  
Go to **DAGs → daily_signal_pipeline → Trigger DAG ▶**.

Wait until `load_prices` starts (it fetches ~2 years of OHLCV — the longest task in the pipeline) — you want to interrupt during an active task.

### 2. Kill the scheduler mid-run

This stack runs `AIRFLOW__CORE__EXECUTOR: LocalExecutor` (see `docker-compose.yml`
x-airflow-common), which executes tasks as subprocesses of the scheduler
itself -- there is **no separate `airflow-worker` Compose service** to stop
(that only exists under `CeleryExecutor`). Killing `airflow-scheduler` kills
the running task subprocesses with it.

```powershell
docker compose stop airflow-scheduler
```

You should see the running tasks freeze in the Airflow UI (tasks stay in "running" state).

### 3. Wait 15 seconds, then restart

```powershell
docker compose start airflow-scheduler
```

Airflow 2.x detects "zombie" tasks (tasks that were running when the process died) and
reschedules them automatically within ~30–60 seconds.

### 4. Watch recovery in the UI

Return to **DAGs → daily_signal_pipeline → (the triggered run) → Graph view**.

Expected behaviour:
- Interrupted tasks are retried automatically (up to 3 attempts each, per DAG config)
- The DAG eventually reaches `success` on all tasks
- No manual intervention required

If a task stays `failed` after all retries, check its logs:  
**Grid view → click the failed task tile → Logs tab**

### 5. Verify data consistency in the DB

```powershell
python scripts/check_pipeline_health.py
```

Expected output:
```
daily_prices  rows=...   tickers=...   latest_date=...   OK
quality_flags rows=...                                    OK
corp_actions  rows=...   tickers=...                     OK
No duplicate (ticker, date) pairs found.
Pipeline health: OK
```

---

## What to check if recovery fails

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Task stuck in `running` after restart | Zombie not detected yet | Wait 60 s; Airflow's zombie detection interval is ~30 s |
| Task fails with `XCom key not found` | Upstream task didn't complete before crash | Manually clear the upstream task in the UI and re-run |
| `write_ohlcv` fails with duplicate key error | Previous partial write left rows; upsert should handle this | Check logs; if persistent, run `make shell-db` and verify `ON CONFLICT` behaviour |
| All tasks immediately succeed on retry | Idempotent upserts worked correctly — this is the desired outcome | Nothing to do |

---

## Idempotency guarantee

All writes use `INSERT ... ON CONFLICT DO UPDATE`, so re-running any task after a crash
is always safe — it will overwrite with identical data and produce the same row count.
This is verifiable by running the DAG twice on the same date and confirming row counts
don't change.

---

## Scheduling notes (Windows Docker Desktop)

- Both DAGs set a timezone-aware `start_date` using `America/New_York`. When
  `start_date` is timezone-aware, Airflow evaluates the cron schedule in that
  timezone before converting execution instants to UTC internally.  Do **not**
  interpret cron fields as UTC.
- **`daily_signal_pipeline`** — `30 21 * * 1-5` → fires at **21:30 ET** weekdays
  (01:30 UTC next day during EDT / 02:30 UTC next day during EST)
- **`daily_paper_trading`** — `0 23 * * 1-5` → fires at **23:00 ET** weekdays
  (03:00 UTC next day during EDT / 04:00 UTC next day during EST); 90-minute gap
  gives the signal pipeline time to complete
- Docker Desktop on Windows suspends containers when the laptop sleeps; the scheduler
  catches up automatically on wake by running any missed intervals
- To confirm catch-up worked: check the DAG grid for backfilled runs marked `success`
