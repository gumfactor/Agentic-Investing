# Airflow Recovery Fire Drill

Validates that the daily pipeline recovers cleanly when Airflow is interrupted mid-run.
Run this before promoting to a live trading environment.

---

## Prerequisites

- Docker stack running (`make up`)
- At least one successful scheduled or manual DAG run already in history
- TimescaleDB populated (`daily_prices` has rows)

---

## Procedure

### 1. Trigger a manual run

Open the Airflow UI at `http://localhost:8080`.  
Go to **DAGs → daily_data_pipeline → Trigger DAG ▶**.

Wait until `fetch_ohlcv` starts (it takes ~30 s) — you want to interrupt during the longest task.

### 2. Kill the scheduler and worker mid-run

```powershell
docker compose stop airflow-scheduler airflow-worker
```

You should see the running tasks freeze in the Airflow UI (tasks stay in "running" state).

### 3. Wait 15 seconds, then restart

```powershell
docker compose start airflow-scheduler airflow-worker
```

Airflow 2.x detects "zombie" tasks (tasks that were running when the process died) and
reschedules them automatically within ~30–60 seconds.

### 4. Watch recovery in the UI

Return to **DAGs → daily_data_pipeline → (the triggered run) → Graph view**.

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

- The scheduler runs inside the container on UTC time
- The DAG schedule `0 20 * * 1-5` fires at 20:00 UTC (weekdays)
- Docker Desktop on Windows suspends containers when the laptop sleeps; the scheduler
  catches up automatically on wake by running any missed intervals
- To confirm catch-up worked: check the DAG grid for backfilled runs marked `success`
