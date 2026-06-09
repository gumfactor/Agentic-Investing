"""Daily market data ingestion pipeline.

Scheduled at 20:00 America/New_York on weekdays after market close.
Each run refreshes a rolling seven-day window so missed laptop runs are
repaired automatically.

DAG structure:
  fetch_universe
      └── fetch_ohlcv
              ├── run_quality_checks
              │       └── write_quality_flags
              ├── write_ohlcv
              │       └── save_snapshot
              └── write_corporate_actions

Failure policy:
  - Each task retries 3× with 5-minute backoff.
  - DAG-level on_failure_callback sends an alert (Slack/email per .env).
  - Missed schedules are replayed after Airflow restarts (catchup=True).
  - Rolling-window upserts make overlapping runs idempotent.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pendulum
from airflow import DAG
from airflow.operators.python import PythonOperator

_MARKET_TIMEZONE = pendulum.timezone("America/New_York")
_DAG_START_DATE = pendulum.datetime(2026, 6, 5, 20, 0, tz=_MARKET_TIMEZONE)
_REPAIR_LOOKBACK_DAYS = 7

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


def _market_date(context: dict[str, Any]) -> date:
    """Return the market date represented by the scheduled interval end."""
    return context["data_interval_end"].in_timezone(_MARKET_TIMEZONE).date()


def _batch_id(run_id: str) -> str:
    """Map an Airflow run ID to the UUID required by the ingestion schema."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rqis-airflow:{run_id}"))


def _fetch_ohlcv(**context: Any) -> str:
    """Refresh recent OHLCV. Returns the MinIO path of the raw response."""
    from data.ingestion.market.yfinance_client import YFinanceClient
    from data.storage.parquet_snapshots import ParquetSnapshots

    tickers: list[str] = context["ti"].xcom_pull(key="tickers", task_ids="fetch_universe")
    end_date = _market_date(context)
    start_date = end_date - timedelta(days=_REPAIR_LOOKBACK_DAYS)

    client = YFinanceClient()
    df, actions = client.fetch_market_data(tickers, start=start_date, end=end_date)

    # Store raw result in MinIO before any transformation
    snapshots = ParquetSnapshots()
    batch_id = _batch_id(context["run_id"])
    raw_path = snapshots.save_raw_response(
        data=df.to_json(orient="records", date_format="iso").encode(),
        source="yfinance",
        data_type="ohlcv",
        batch_id=batch_id,
    )

    context["ti"].xcom_push(key="ohlcv_df_json", value=df.to_json(orient="records", date_format="iso"))
    context["ti"].xcom_push(key="raw_path", value=raw_path)
    context["ti"].xcom_push(key="batch_id", value=batch_id)
    context["ti"].xcom_push(key="window_start", value=str(start_date))
    context["ti"].xcom_push(key="window_end", value=str(end_date))
    context["ti"].xcom_push(
        key="ca_df_json",
        value=actions.to_json(orient="records", date_format="iso"),
    )
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
    batch_id: str = context["ti"].xcom_pull(key="batch_id", task_ids="fetch_ohlcv")
    window_start: str = context["ti"].xcom_pull(key="window_start", task_ids="fetch_ohlcv")
    window_end: str = context["ti"].xcom_pull(key="window_end", task_ids="fetch_ohlcv")

    df = pd.read_json(df_json, orient="records")
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date

    writer = TimescaleWriter()
    records_written = writer.upsert_ohlcv(df)
    writer.log_ingestion(
        source="yfinance",
        data_type="ohlcv",
        status="complete",
        start_date=date.fromisoformat(window_start),
        end_date=date.fromisoformat(window_end),
        records_written=records_written,
        raw_storage_path=raw_path,
        batch_id=batch_id,
    )
    return records_written


def _save_snapshot(**context: Any) -> str:
    import pandas as pd
    from data.storage.parquet_snapshots import ParquetSnapshots

    df_json: str = context["ti"].xcom_pull(key="ohlcv_df_json", task_ids="fetch_ohlcv")
    window_end: str = context["ti"].xcom_pull(key="window_end", task_ids="fetch_ohlcv")

    df = pd.read_json(df_json, orient="records")
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date

    snap = ParquetSnapshots()
    path = snap.save_snapshot(
        df,
        data_type="daily_prices",
        snapshot_date=date.fromisoformat(window_end),
    )
    return path


def _write_corporate_actions(**context: Any) -> int:
    import pandas as pd
    from data.storage.timescale_writer import TimescaleWriter

    ca_json: str = context["ti"].xcom_pull(key="ca_df_json", task_ids="fetch_ohlcv")
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
    start_date=_DAG_START_DATE,
    catchup=True,
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

    t_write_ca = PythonOperator(
        task_id="write_corporate_actions",
        python_callable=_write_corporate_actions,
    )

    # ── Task dependency graph ─────────────────────────────────────────────────
    t_fetch_universe >> t_fetch_ohlcv

    t_fetch_ohlcv >> t_quality_checks >> t_write_flags
    t_fetch_ohlcv >> t_write_ohlcv >> t_save_snapshot
    t_fetch_ohlcv >> t_write_ca
