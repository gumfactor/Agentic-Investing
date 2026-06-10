"""Daily signal computation pipeline.

Runs after the daily_data_pipeline has written fresh OHLCV data.
Computes factor scores for all available factors and writes composite
alpha_scores to the database.

Scheduled at 21:30 ET weekdays (90 minutes after market-data pipeline
at 20:00 ET, giving the data pipeline time to complete).

DAG structure:
  load_prices
      ├── compute_momentum
      ├── compute_lowvol
      └── (compute_value — skipped if no fundamentals)
              └── combine_scores
                      └── write_scores

Graceful degradation
--------------------
Value and quality factors depend on fundamentals from financial_statements.
If that table is empty (fundamentals not yet backfilled), those tasks return
empty DataFrames and the composite uses only price-based factors.  No alert
is raised — this is expected until Phase 2 fundamentals backfill runs.

Strategy ID
-----------
The strategy_id written to factor_scores and alpha_scores is controlled by
the DAG param 'strategy_id'.  Default: 'v1_base_momentum'.  Per C6: never
modify a strategy config that has been used in a live session — create a
new version instead.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pendulum
from airflow import DAG
from airflow.operators.python import PythonOperator

_MARKET_TIMEZONE = pendulum.timezone("America/New_York")
_DAG_START_DATE = pendulum.datetime(2026, 6, 9, 21, 30, tz=_MARKET_TIMEZONE)
# 12-month momentum needs 252 trading days plus the 21-day skip window.
_PRICE_LOOKBACK_DAYS = 450
_DEFAULT_STRATEGY_ID = "v1_base_momentum"
# All factors remain persisted for diagnostics. Only momentum currently clears
# the frozen held-out IC and HAC significance gates for production alpha.
_COMPOSITE_FACTOR_WEIGHTS = {
    "momentum": 1.0,
}

_default_args: dict[str, Any] = {
    "owner": "rqis",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


# ─── Task functions ───────────────────────────────────────────────────────────

def _load_prices(**context: Any) -> None:
    """Load recent OHLCV prices from TimescaleDB and push to XCom."""
    import pandas as pd
    from sqlalchemy import create_engine, text
    import os

    end_date = context["data_interval_end"].in_timezone(_MARKET_TIMEZONE).date()
    start_date = end_date - timedelta(days=_PRICE_LOOKBACK_DAYS)

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT ticker, date, close::float AS close "
                "FROM daily_prices "
                "WHERE date BETWEEN :start AND :end "
                "ORDER BY ticker, date"
            ),
            conn,
            params={"start": start_date, "end": end_date},
        )

    context["ti"].xcom_push(key="prices_json", value=df.to_json(orient="records", date_format="iso"))
    context["ti"].xcom_push(key="score_date", value=str(end_date))


def _compute_momentum(**context: Any) -> None:
    import pandas as pd
    from signals.factors.momentum import compute_momentum_scores

    prices_json: str = context["ti"].xcom_pull(key="prices_json", task_ids="load_prices")
    prices = pd.read_json(prices_json, orient="records", convert_dates=False)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    scores = compute_momentum_scores(prices)
    context["ti"].xcom_push(
        key="momentum_scores_json",
        value=scores.to_json(orient="records", date_format="iso") if not scores.empty else "[]",
    )


def _compute_lowvol(**context: Any) -> None:
    import pandas as pd
    from signals.factors.low_vol import compute_lowvol_scores

    prices_json: str = context["ti"].xcom_pull(key="prices_json", task_ids="load_prices")
    prices = pd.read_json(prices_json, orient="records", convert_dates=False)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    scores = compute_lowvol_scores(prices)
    context["ti"].xcom_push(
        key="lowvol_scores_json",
        value=scores.to_json(orient="records", date_format="iso") if not scores.empty else "[]",
    )


def _compute_value(**context: Any) -> None:
    """Compute value scores.  Skips gracefully if fundamentals table is empty."""
    import pandas as pd
    from sqlalchemy import create_engine, text
    import os
    from signals.factors.value import compute_value_scores

    score_date_str: str = context["ti"].xcom_pull(key="score_date", task_ids="load_prices")
    score_date = date.fromisoformat(score_date_str)

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        result = conn.execute(text("SELECT COUNT(*) FROM financial_statements"))
        row_count = result.scalar()

    if row_count == 0:
        context["ti"].xcom_push(key="value_scores_json", value="[]")
        return

    with engine.connect() as conn:
        fund = pd.read_sql(
            text(
                "SELECT ticker, period_end_date, release_date, period_type, "
                "item_name, value::float AS value "
                "FROM financial_statements "
                "WHERE release_date <= :as_of "
                "AND period_end_date >= :cutoff "
                "AND item_name IN ('net_income', 'total_equity', 'free_cash_flow', 'shares_outstanding')"
            ),
            conn,
            params={"as_of": score_date, "cutoff": score_date - timedelta(days=550)},
        )
    fund["period_end_date"] = pd.to_datetime(fund["period_end_date"]).dt.date
    fund["release_date"] = pd.to_datetime(fund["release_date"]).dt.date

    prices_json: str = context["ti"].xcom_pull(key="prices_json", task_ids="load_prices")
    prices = pd.read_json(prices_json, orient="records", convert_dates=False)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    scores = compute_value_scores(fund, prices, score_dates=[score_date])
    context["ti"].xcom_push(
        key="value_scores_json",
        value=scores.to_json(orient="records", date_format="iso") if not scores.empty else "[]",
    )


def _compute_quality(**context: Any) -> None:
    """Compute quality scores.  Skips gracefully if fundamentals table is empty."""
    import pandas as pd
    from sqlalchemy import create_engine, text
    import os
    from signals.factors.quality import compute_quality_scores

    score_date_str: str = context["ti"].xcom_pull(key="score_date", task_ids="load_prices")
    score_date = date.fromisoformat(score_date_str)

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        row_count = conn.execute(text("SELECT COUNT(*) FROM financial_statements")).scalar()

    if row_count == 0:
        context["ti"].xcom_push(key="quality_scores_json", value="[]")
        return

    with engine.connect() as conn:
        fund = pd.read_sql(
            text(
                "SELECT ticker, period_end_date, release_date, period_type, "
                "item_name, value::float AS value "
                "FROM financial_statements "
                "WHERE release_date <= :as_of "
                "AND period_end_date >= :cutoff "
                "AND item_name IN ('net_income', 'total_equity', 'total_assets', 'gross_profit', 'operating_cash_flow')"
            ),
            conn,
            params={"as_of": score_date, "cutoff": score_date - timedelta(days=550)},
        )
    fund["period_end_date"] = pd.to_datetime(fund["period_end_date"]).dt.date
    fund["release_date"] = pd.to_datetime(fund["release_date"]).dt.date

    prices_json: str = context["ti"].xcom_pull(key="prices_json", task_ids="load_prices")
    prices = pd.read_json(prices_json, orient="records", convert_dates=False)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    scores = compute_quality_scores(fund, prices, score_dates=[score_date])
    context["ti"].xcom_push(
        key="quality_scores_json",
        value=scores.to_json(orient="records", date_format="iso") if not scores.empty else "[]",
    )


def _combine_scores(**context: Any) -> None:
    """Combine factor scores into composite alpha_scores."""
    import pandas as pd
    from signals.scoring.scorer import combine_factor_scores

    strategy_id = context["params"].get("strategy_id", _DEFAULT_STRATEGY_ID)
    score_date_str: str = context["ti"].xcom_pull(key="score_date", task_ids="load_prices")
    if score_date_str is None:
        raise ValueError("score_date XCom missing — load_prices likely failed")
    score_date = date.fromisoformat(score_date_str)

    def _load(key: str, task_id: str) -> pd.DataFrame:
        raw = context["ti"].xcom_pull(key=key, task_ids=task_id)
        if not raw or raw == "[]":
            return pd.DataFrame()
        df = pd.read_json(raw, orient="records", convert_dates=False)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    factor_scores = {}
    score_col_map = {}

    momentum = _load("momentum_scores_json", "compute_momentum")
    if not momentum.empty:
        factor_scores["momentum"] = momentum
        score_col_map["momentum"] = "momentum_score"

    lowvol = _load("lowvol_scores_json", "compute_lowvol")
    if not lowvol.empty:
        factor_scores["lowvol"] = lowvol
        score_col_map["lowvol"] = "lowvol_score"

    value = _load("value_scores_json", "compute_value")
    if not value.empty:
        factor_scores["value"] = value
        score_col_map["value"] = "value_score"

    quality = _load("quality_scores_json", "compute_quality")
    if not quality.empty:
        factor_scores["quality"] = quality
        score_col_map["quality"] = "quality_score"

    factor_df, alpha_df = combine_factor_scores(
        factor_scores=factor_scores,
        score_col_map=score_col_map,
        strategy_id=strategy_id,
        score_date=score_date,
        weights=_COMPOSITE_FACTOR_WEIGHTS,
    )

    context["ti"].xcom_push(
        key="factor_scores_json",
        value=factor_df.to_json(orient="records", date_format="iso") if not factor_df.empty else "[]",
    )
    context["ti"].xcom_push(
        key="alpha_scores_json",
        value=alpha_df.to_json(orient="records", date_format="iso") if not alpha_df.empty else "[]",
    )


def _write_scores(**context: Any) -> dict:
    """Persist factor_scores and alpha_scores to TimescaleDB."""
    import pandas as pd
    from sqlalchemy import create_engine, text
    import os

    def _load(key: str) -> pd.DataFrame:
        raw = context["ti"].xcom_pull(key=key, task_ids="combine_scores")
        if not raw or raw == "[]":
            return pd.DataFrame()
        df = pd.read_json(raw, orient="records", convert_dates=False)
        if "score_date" in df.columns:
            df["score_date"] = pd.to_datetime(df["score_date"]).dt.date
        return df

    factor_df = _load("factor_scores_json")
    alpha_df = _load("alpha_scores_json")

    if factor_df.empty and alpha_df.empty:
        return {"factor_rows": 0, "alpha_rows": 0}

    engine = create_engine(os.environ["DATABASE_URL"])

    factor_count = 0
    alpha_count = 0

    # Single transaction: both tables commit together or neither does
    with engine.begin() as conn:
        if not factor_df.empty:
            factor_records = factor_df.to_dict("records")
            conn.execute(
                text(
                    "INSERT INTO factor_scores "
                    "(ticker, score_date, factor_name, strategy_id, z_score, raw_value) "
                    "VALUES (:ticker, :score_date, :factor_name, :strategy_id, "
                    ":z_score, :raw_value) "
                    "ON CONFLICT (ticker, score_date, factor_name, strategy_id) "
                    "DO UPDATE SET z_score = EXCLUDED.z_score, "
                    "raw_value = EXCLUDED.raw_value, "
                    "computed_at = NOW()"
                ),
                factor_records,
            )
            factor_count = len(factor_records)

        if not alpha_df.empty:
            alpha_records = alpha_df.to_dict("records")
            conn.execute(
                text(
                    "INSERT INTO alpha_scores "
                    "(ticker, score_date, strategy_id, alpha_score, rank, universe_size) "
                    "VALUES (:ticker, :score_date, :strategy_id, :alpha_score, "
                    ":rank, :universe_size) "
                    "ON CONFLICT (ticker, score_date, strategy_id) "
                    "DO UPDATE SET alpha_score = EXCLUDED.alpha_score, "
                    "rank = EXCLUDED.rank, "
                    "universe_size = EXCLUDED.universe_size, "
                    "computed_at = NOW()"
                ),
                alpha_records,
            )
            alpha_count = len(alpha_records)

    return {"factor_rows": factor_count, "alpha_rows": alpha_count}


# ─── DAG definition ──────────────────────────────────────────────────────────

with DAG(
    dag_id="daily_signal_pipeline",
    default_args=_default_args,
    description="Daily factor scoring and composite alpha computation",
    schedule_interval="30 21 * * 1-5",   # 9:30 PM ET weekdays
    start_date=_DAG_START_DATE,
    catchup=False,
    max_active_runs=1,
    params={"strategy_id": _DEFAULT_STRATEGY_ID},
    tags=["signals", "phase-2"],
) as dag:

    t_load_prices = PythonOperator(
        task_id="load_prices",
        python_callable=_load_prices,
    )

    t_momentum = PythonOperator(
        task_id="compute_momentum",
        python_callable=_compute_momentum,
    )

    t_lowvol = PythonOperator(
        task_id="compute_lowvol",
        python_callable=_compute_lowvol,
    )

    t_value = PythonOperator(
        task_id="compute_value",
        python_callable=_compute_value,
    )

    t_quality = PythonOperator(
        task_id="compute_quality",
        python_callable=_compute_quality,
    )

    t_combine = PythonOperator(
        task_id="combine_scores",
        python_callable=_combine_scores,
        trigger_rule="none_failed",  # run if no upstream failures; allows skipped tasks
    )

    t_write = PythonOperator(
        task_id="write_scores",
        python_callable=_write_scores,
    )

    # ── Task dependency graph ─────────────────────────────────────────────────
    t_load_prices >> [t_momentum, t_lowvol, t_value, t_quality]
    [t_momentum, t_lowvol, t_value, t_quality] >> t_combine
    t_combine >> t_write
