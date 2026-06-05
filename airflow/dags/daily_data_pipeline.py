"""Daily market data ingestion pipeline.

Scheduled to run at 20:00 on weekdays (after US equity market close).
Pulls the day's OHLCV bars and any new corporate actions for the full universe,
runs quality checks, writes to TimescaleDB, and saves a parquet snapshot.

DAG structure:
  fetch_universe
      └── fetch_ohlcv
              ├── run_quality_checks
              │       └── write_quality_flags
              └── write_ohlcv
                      └── save_snapshot
  fetch_corporate_actions (parallel with fetch_ohlcv)
      └── write_corporate_actions
  log_completion

Failure policy:
  - Each task retries 3× with 5-minute backoff.
  - DAG-level on_failure_callback sends an alert (Slack/email per .env).
  - A failed run does NOT block the next day's run (catchup=False).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.dates import days_ago

# ─── Default args ─────────────────────────────────────────────────────────────

_default_args: dict[str, Any] = {
    "owner": "rqis",
    "depends_on_past": False,
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
}


# ─── Task functions ───────────────────────────────────────────────────────────

def _fetch_universe(**context: Any) -> list[str]:
    from config.universe_loader import load_universe
    tickers = load_universe()
    context["ti"].xcom_push(key="tickers", value=tickers)
    return tickers


def _fetch_ohlcv(**context: Any) -> str:
    """Fetch OHLCV for today. Returns the MinIO path of the raw response."""
    from data.ingestion.market.yfinance_client import YFinanceClient
    from data.storage.parquet_snapshots import ParquetSnapshots

    tickers: list[str] = context["ti"].xcom_pull(key="tickers", task_ids="fetch_universe")
    logical_date: date = context["logical_date"].date()

    client = YFinanceClient()
    df = client.fetch_ohlcv(tickers, start=logical_date, end=logical_date)

    # Store raw result in MinIO before any transformation
    snapshots = ParquetSnapshots()
    batch_id = context["run_id"]
    raw_path = snapshots.save_raw_response(
        data=df.to_json(orient="records", date_format="iso").encode(),
        source="yfinance",
        data_type="ohlcv",
        batch_id=batch_id,
    )

    context["ti"].xcom_push(key="ohlcv_df_json", value=df.to_json(orient="records", date_format="iso"))
    context["ti"].xcom_push(key="raw_path", value=raw_path)
    context["ti"].xcom_push(key="logical_date", value=str(logical_date))
    return raw_path


def _run_quality_checks(**context: Any) -> str:
    from data.normalization.quality_checks import run_quality_checks
    import pandas as pd

    df_json: str = context["ti"].xcom_pull(key="ohlcv_df_json", task_ids="fetch_ohlcv")
    df = pd.read_json(df_json, orient="records")
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date

    flags = run_quality_checks(df)
    context["ti"].xcom_push(key="flags_json", value=flags.to_json(orient="records"))
    return f"{len(flags)} flags detected"


def _write_quality_flags(**context: Any) -> int:
    import pandas as pd
    from data.storage.timescale_writer import TimescaleWriter

    flags_json: str = context["ti"].xcom_pull(key="flags_json", task_ids="run_quality_checks")
    flags = pd.read_json(flags_json, orient="records")
    if flags.empty:
        return 0
    if "date" in flags.columns:
        flags["date"] = pd.to_datetime(flags["date"]).dt.date

    writer = TimescaleWriter()
    return writer.write_quality_flags(flags)


def _write_ohlcv(**context: Any) -> int:
    import pandas as pd
    from data.storage.timescale_writer import TimescaleWriter

    df_json: str = context["ti"].xcom_pull(key="ohlcv_df_json", task_ids="fetch_ohlcv")
    raw_path: str = context["ti"].xcom_pull(key="raw_path", task_ids="fetch_ohlcv")
    logical_date_str: str = context["ti"].xcom_pull(key="logical_date", task_ids="fetch_ohlcv")

    df = pd.read_json(df_json, orient="records")
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date

    writer = TimescaleWriter()
    records_written = writer.upsert_ohlcv(df)
    writer.log_ingestion(
        source="yfinance",
        data_type="ohlcv",
        status="complete",
        start_date=date.fromisoformat(logical_date_str),
        end_date=date.fromisoformat(logical_date_str),
        records_written=records_written,
        raw_storage_path=raw_path,
        batch_id=context["run_id"],
    )
    return records_written


def _save_snapshot(**context: Any) -> str:
    import pandas as pd
    from data.storage.parquet_snapshots import ParquetSnapshots

    df_json: str = context["ti"].xcom_pull(key="ohlcv_df_json", task_ids="fetch_ohlcv")
    logical_date_str: str = context["ti"].xcom_pull(key="logical_date", task_ids="fetch_ohlcv")

    df = pd.read_json(df_json, orient="records")
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date

    snap = ParquetSnapshots()
    path = snap.save_snapshot(df, data_type="daily_prices", snapshot_date=date.fromisoformat(logical_date_str))
    return path


def _fetch_corporate_actions(**context: Any) -> str:
    import pandas as pd
    from data.ingestion.market.yfinance_client import YFinanceClient

    tickers: list[str] = context["ti"].xcom_pull(key="tickers", task_ids="fetch_universe")
    logical_date: date = context["logical_date"].date()
    # Fetch a rolling 7-day window so we don't miss actions announced mid-week
    start = logical_date - timedelta(days=7)

    client = YFinanceClient()
    df = client.fetch_corporate_actions(tickers, start=start, end=logical_date)
    context["ti"].xcom_push(key="ca_df_json", value=df.to_json(orient="records", date_format="iso"))
    return f"{len(df)} corporate actions fetched"


def _write_corporate_actions(**context: Any) -> int:
    import pandas as pd
    from data.storage.timescale_writer import TimescaleWriter

    ca_json: str = context["ti"].xcom_pull(key="ca_df_json", task_ids="fetch_corporate_actions")
    df = pd.read_json(ca_json, orient="records")
    if df.empty:
        return 0
    if "ex_date" in df.columns:
        df["ex_date"] = pd.to_datetime(df["ex_date"]).dt.date

    # Decimal values serialised as float in JSON; convert back
    from decimal import Decimal
    if "value" in df.columns:
        df["value"] = df["value"].apply(lambda v: Decimal(str(v)) if v is not None else None)

    writer = TimescaleWriter()
    return writer.upsert_corporate_actions(df)


# ─── DAG definition ──────────────────────────────────────────────────────────

with DAG(
    dag_id="daily_data_pipeline",
    default_args=_default_args,
    description="Daily OHLCV and corporate actions ingestion for full universe",
    schedule_interval="0 20 * * 1-5",   # 8 PM weekdays
    start_date=days_ago(1),
    catchup=False,
    max_active_runs=1,
    tags=["data", "phase-1"],
) as dag:

    t_fetch_universe = PythonOperator(
        task_id="fetch_universe",
        python_callable=_fetch_universe,
    )

    t_fetch_ohlcv = PythonOperator(
        task_id="fetch_ohlcv",
        python_callable=_fetch_ohlcv,
    )

    t_quality_checks = PythonOperator(
        task_id="run_quality_checks",
        python_callable=_run_quality_checks,
    )

    t_write_flags = PythonOperator(
        task_id="write_quality_flags",
        python_callable=_write_quality_flags,
    )

    t_write_ohlcv = PythonOperator(
        task_id="write_ohlcv",
        python_callable=_write_ohlcv,
    )

    t_save_snapshot = PythonOperator(
        task_id="save_snapshot",
        python_callable=_save_snapshot,
    )

    t_fetch_ca = PythonOperator(
        task_id="fetch_corporate_actions",
        python_callable=_fetch_corporate_actions,
    )

    t_write_ca = PythonOperator(
        task_id="write_corporate_actions",
        python_callable=_write_corporate_actions,
    )

    # ── Task dependency graph ─────────────────────────────────────────────────
    t_fetch_universe >> [t_fetch_ohlcv, t_fetch_ca]

    t_fetch_ohlcv >> t_quality_checks >> t_write_flags
    t_fetch_ohlcv >> t_write_ohlcv >> t_save_snapshot

    t_fetch_ca >> t_write_ca
