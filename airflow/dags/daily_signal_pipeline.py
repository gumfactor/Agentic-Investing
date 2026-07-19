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

    # PIT cross-section BEFORE factor computation (BUG-008 / Codex PR #34
    # P2): the factor tasks z-score across every ticker in this price panel,
    # so an ineligible-but-priced ticker must be removed HERE, not after
    # scoring. Filtering by ticker (keeping each eligible ticker's full
    # lookback history) preserves rolling windows. Degrades to the
    # unfiltered (provisional) panel when no published universe import
    # covers score_date — BUG-069.
    import structlog as _structlog

    _log = _structlog.get_logger("rqis.airflow")
    try:
        _eligible = _pit_eligible_tickers_sql(os.environ["DATABASE_URL"], end_date)
    except Exception as _exc:  # deliberate broad degrade — see _pit_membership_filter
        _eligible = None
        _log.warning("pit_universe_lookup_failed_scores_provisional", error=str(_exc))
    if _eligible is not None:
        n_before = df["ticker"].nunique()
        df = df[df["ticker"].isin(_eligible)].reset_index(drop=True)
        _log.info(
            "pit_price_panel_filtered",
            score_date=str(end_date),
            tickers_before=n_before,
            tickers_after=df["ticker"].nunique(),
        )
    else:
        _log.warning(
            "pit_universe_unavailable_scores_provisional",
            score_date=str(end_date),
            note=(
                "factor cross-section NOT filtered to point-in-time members; "
                "daily scores are provisional for research (BUG-008/BUG-069)"
            ),
        )

    # ── Corporate-action cutoff-aware adjustment (BUG-009 section 2.3) ───────
    # Price-ratio-based factors (momentum, lowvol — computed by
    # _compute_momentum/_compute_lowvol below) must use only actions known-
    # and-occurred by THIS run's own score_date cutoff (design plan section
    # 2.3). Unlike scripts/validate_signal_ic.py's backfill (which computes
    # many score dates in one vectorized pass and therefore documents a
    # single-run-boundary-cutoff approximation — BUG-071), this DAG scores
    # exactly ONE date (end_date) per run, so score_cutoff =
    # session_close_cutoff(end_date) is the exact, fully correct per-date
    # cutoff with no residual gap. Value/quality (_compute_value/_compute_
    # quality) deliberately keep pulling raw prices via "prices_json" — a
    # valuation ratio needs the actual traded price, not a total-return-
    # adjusted synthetic one (same rationale as validate_signal_ic.py).
    from data.normalization.corporate_actions import build_score_price_history_as_of
    from data.universe.calendar import session_close_cutoff

    with engine.connect() as conn:
        corporate_actions = pd.read_sql(
            text(
                "SELECT ticker, ex_date, action_type, value, known_at, source_version "
                "FROM corporate_actions ORDER BY ticker, ex_date"
            ),
            conn,
        )
    corporate_actions["ex_date"] = pd.to_datetime(corporate_actions["ex_date"]).dt.date
    corporate_actions["known_at"] = pd.to_datetime(corporate_actions["known_at"], utc=True)

    score_cutoff = session_close_cutoff(end_date)
    adjusted_df, adj_meta = build_score_price_history_as_of(
        df, corporate_actions, score_cutoff=score_cutoff
    )
    _log.info(
        "score_price_history_adjusted",
        score_date=str(end_date),
        score_cutoff=score_cutoff.isoformat(),
        n_actions_considered=adj_meta.n_actions_considered,
        n_actions_excluded_by_cutoff=adj_meta.n_actions_excluded_by_cutoff,
        n_actions_excluded_missing_known_at=adj_meta.n_actions_excluded_missing_known_at,
    )
    adjusted_df = adjusted_df[["ticker", "date", "adj_close"]].rename(columns={"adj_close": "close"})
    adjusted_df["close"] = adjusted_df["close"].astype(float)

    context["ti"].xcom_push(key="prices_json", value=df.to_json(orient="records", date_format="iso"))
    context["ti"].xcom_push(
        key="adjusted_prices_json", value=adjusted_df.to_json(orient="records", date_format="iso")
    )
    context["ti"].xcom_push(key="score_date", value=str(end_date))
    # Adversarial-review round 11 (BUG-008/BUG-009 section 4): _write_scores
    # needs to know whether PIT membership filtering actually succeeded for
    # THIS score_date -- _eligible is None on a degraded run (BUG-069), and
    # _write_scores must not tag those rows with a research_run_id whose
    # methodology claims PIT-universe safety. Pushed as an explicit XCom
    # flag since Airflow tasks are separate invocations with no shared
    # in-process state.
    context["ti"].xcom_push(key="pit_universe_applied", value=_eligible is not None)


def _compute_momentum(**context: Any) -> None:
    import pandas as pd
    from signals.composites.momentum_score import compute_momentum_scores

    # Split/dividend-adjusted (BUG-009 section 2.3): momentum is a pure
    # price-ratio indicator, so it must use build_score_price_history_as_of's
    # cutoff-aware series (adjusted_prices_json), not the raw prices_json
    # value/quality use.
    prices_json: str = context["ti"].xcom_pull(key="adjusted_prices_json", task_ids="load_prices")
    prices = pd.read_json(prices_json, orient="records", convert_dates=False)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    scores = compute_momentum_scores(prices)
    context["ti"].xcom_push(
        key="momentum_scores_json",
        value=scores.to_json(orient="records", date_format="iso") if not scores.empty else "[]",
    )


def _compute_lowvol(**context: Any) -> None:
    import pandas as pd
    from signals.composites.low_vol_score import compute_lowvol_scores

    # Split/dividend-adjusted (BUG-009 section 2.3): low-vol is a pure
    # price-ratio indicator (volatility of returns), same rationale as
    # _compute_momentum above.
    prices_json: str = context["ti"].xcom_pull(key="adjusted_prices_json", task_ids="load_prices")
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
    from signals.composites.value_score import compute_value_scores

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
    from signals.composites.quality_score import compute_quality_scores

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

    # PIT cross-section for quality (BUG-008 / Codex PR #34 P2): unlike the
    # price-based factors (and value, whose ratios join per-ticker closes
    # from the already-filtered price panel), quality ratios use ONLY
    # fundamentals — so a non-member with valid financial_statements rows
    # would still enter the per-date z-scores even though _load_prices
    # filtered the price panel. Pass the eligibility frame so the composite
    # masks the cross-section BEFORE z-scoring; degrade to the provisional
    # unfiltered cross-section when no published import covers score_date
    # (BUG-069), matching the panel filter's behavior.
    eligibility = None
    try:
        _eligible = _pit_eligible_tickers_sql(os.environ["DATABASE_URL"], score_date)
    except Exception as _exc:  # deliberate broad degrade — see _pit_membership_filter
        _eligible = None
        import structlog as _structlog

        _structlog.get_logger("rqis.airflow").warning(
            "pit_universe_lookup_failed_quality_provisional", error=str(_exc)
        )
    if _eligible is not None:
        eligibility = pd.DataFrame(
            [{"ticker": t, "date": score_date} for t in sorted(_eligible)]
        )

    scores = compute_quality_scores(
        fund, prices, score_dates=[score_date], eligibility=eligibility
    )
    context["ti"].xcom_push(
        key="quality_scores_json",
        value=scores.to_json(orient="records", date_format="iso") if not scores.empty else "[]",
    )
    # Adversarial-review round 11 (BUG-008/BUG-009 section 4): quality does
    # its OWN independent PIT lookup (unlike momentum/lowvol/value, which
    # all rely solely on _load_prices' single panel filter) -- so its
    # degrade state must be surfaced to _write_scores separately. See
    # _load_prices' equivalent "pit_universe_applied" XCom push.
    context["ti"].xcom_push(key="quality_pit_universe_applied", value=_eligible is not None)


def _pit_eligible_tickers_sql(database_url: str, score_date: date) -> set[str] | None:
    """Eligible sp500 tickers as of score_date via plain SQL, or None.

    SQLAlchemy-1.4-compatible ON PURPOSE (Codex PR #34 P1): the Airflow
    runtime image (infra/docker/Dockerfile.airflow) pins SQLAlchemy 1.4.51,
    while data.universe.models uses SQLAlchemy 2-only APIs
    (DeclarativeBase/mapped_column). Importing data.universe.runtime here
    would raise at import time inside the packaged DAG environment — before
    any graceful fallback could run — so this function reimplements the
    runtime eligibility predicate with text() queries only. It MUST stay in
    semantic lockstep with data.universe.runtime._interval_confers_eligibility:
    entry side known_at <= cutoff; exit side (end IS NULL, or as_of < end,
    or end_known_at > cutoff). tests/test_daily_signal_pipeline_pit.py
    asserts both the import isolation and the semantic parity.

    Returns None when no published import covers score_date (caller
    degrades to provisional scores).
    """
    from datetime import datetime, timezone

    from sqlalchemy import create_engine, text

    # Session-close observation cutoff, mirroring
    # data.universe.calendar.session_close_cutoff (21:00 UTC — the
    # conservative year-round choice documented there).
    cutoff = datetime(
        score_date.year, score_date.month, score_date.day, 21, 0, 0, tzinfo=timezone.utc
    )

    def _as_date(value: Any) -> date:
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])

    def _as_utc(value: Any) -> datetime:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    # Rows are selected plainly and the eligibility predicate is applied in
    # Python: SQL-side date/datetime comparisons are dialect-fragile, and
    # this keeps the logic byte-for-byte comparable with the runtime's
    # _interval_confers_eligibility.
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            batch = conn.execute(
                text(
                    "SELECT id, coverage_start, coverage_end "
                    "FROM universe_import_batches "
                    "WHERE universe_id = 'sp500' AND status = 'published' "
                    "ORDER BY published_at DESC LIMIT 1"
                )
            ).fetchone()
            if batch is None:
                return None
            batch_id = batch[0]
            if not (_as_date(batch[1]) <= score_date <= _as_date(batch[2])):
                return None
            rows = conn.execute(
                text(
                    "SELECT ticker, effective_start, effective_end, known_at, end_known_at "
                    "FROM universe_membership "
                    "WHERE universe_id = 'sp500' AND import_batch_id = :batch_id"
                ),
                {"batch_id": batch_id},
            ).fetchall()
    finally:
        engine.dispose()

    eligible: set[str] = set()
    for ticker, effective_start, effective_end, known_at, end_known_at in rows:
        if _as_date(effective_start) > score_date:
            continue
        if _as_utc(known_at) > cutoff:
            continue
        if effective_end is None or score_date < _as_date(effective_end):
            eligible.add(ticker)
        elif end_known_at is not None and _as_utc(end_known_at) > cutoff:
            eligible.add(ticker)
    return eligible


def _pit_membership_filter(df: Any, score_date: date) -> Any:
    """Filter a factor-score DataFrame to knowable index members (BUG-008).

    Uses the point-in-time universe when a published import covers
    score_date. This DAG's same-day output is OPERATIONAL (it feeds the
    paper pipeline), so a missing/stale universe import — or any lookup
    failure (missing tables, DB drift) — degrades to a loud warning instead
    of failing the daily run; the emitted scores are then PROVISIONAL for
    research purposes, exactly like the pre-01B-2 behavior (BUG-069).
    Historical research callers (IC validation, backfills) use the
    fail-closed path in data.universe.runtime directly.
    """
    import os

    import structlog

    log = structlog.get_logger("rqis.airflow")
    try:
        eligible = _pit_eligible_tickers_sql(os.environ["DATABASE_URL"], score_date)
        degrade_reason = "no published universe import covers score_date"
    except Exception as exc:  # deliberate broad degrade — see docstring
        eligible = None
        degrade_reason = f"universe lookup failed: {exc}"
    if eligible is None:
        log.warning(
            "pit_universe_unavailable_scores_provisional",
            score_date=str(score_date),
            reason=degrade_reason,
            note=(
                "daily scores emitted WITHOUT point-in-time membership filtering; "
                "provisional for research (BUG-008). Run "
                "scripts/import_universe_membership.py to advance coverage."
            ),
        )
        return df
    if df.empty:
        return df
    n_before = len(df)
    filtered = df[df["ticker"].isin(eligible)].reset_index(drop=True)
    if len(filtered) != n_before:
        log.info(
            "pit_membership_filter_applied",
            score_date=str(score_date),
            rows_before=n_before,
            rows_after=len(filtered),
        )
    return filtered


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
        # Point-in-time membership filter (BUG-008 / 01B-2); degrades to a
        # warning when no published universe import covers score_date.
        return _pit_membership_filter(df, score_date)

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


def _adjusted_closes_for_simulation(
    prices_df: Any,
    engine: Any,
    sim_date: date,
    prev_date: date,
    today_closes: Any,
    prev_day_closes: Any,
) -> tuple[Any, Any]:
    """Corporate-action-adjusted (ticker -> close) series for sim_date and
    prev_date, used by :func:`_write_simulation` to compute a close-to-close
    daily return (BUG-009 section 2.3, adversarial-review round 10
    self-audit sweep).

    Before this fix, ``_write_simulation`` divided RAW close-to-close
    prices with no corporate-action adjustment. A split/dividend landing
    exactly on sim_date (or prev_date) would inject a fabricated single-day
    loss/gain into a strategy's simulated return -- and because
    ``simulated_nav`` COMPOUNDS from the prior row (see
    ``_write_simulation``'s docstring), that one bad day permanently
    distorts every later NAV row for the strategy, not just the affected
    day.

    Reuses :func:`data.normalization.corporate_actions
    .build_realized_total_return_as_of` (the same builder
    ``signals.research.ic.compute_realized_forward_returns_as_of`` uses)
    with ``entry_date=prev_date`` and
    ``exit_cutoff=session_close_cutoff(sim_date)`` -- ``_write_simulation``
    runs exactly one sim_date per DAG run, so that cutoff is the exact,
    fully correct per-date cutoff (same reasoning as the ``score_cutoff``
    comment in ``_load_prices`` for momentum/lowvol, not an approximation).
    Passes the FULL price history (not just [prev_date, sim_date]) because
    a dividend's per-share factor is looked up from the close ON ITS OWN
    ex_date, which is not necessarily prev_date or sim_date -- truncating
    the price panel would silently drop that dividend's adjustment.

    Non-blocking degrade-to-raw on any adjustment failure (missing
    corporate_actions table, query error, etc.), consistent with
    ``_write_simulation``'s existing "log and skip, never abort the
    pipeline" philosophy -- ``strategy_simulations`` is a
    diagnostic/dashboard table, not a research_run_id-tagged research
    result (no such column exists on ``strategy_simulations``), so there is
    no provenance-honesty claim (BUG-009 section 4) to violate by
    degrading.

    Args:
        today_closes, prev_day_closes: the RAW (ticker -> close) fallback
            series, returned unchanged if adjustment is unavailable.

    Returns:
        (today_closes, prev_day_closes), each corporate-action-adjusted
        when adjustment succeeds, otherwise the raw inputs unchanged.
    """
    import pandas as pd
    from sqlalchemy import text as _text

    try:
        from data.normalization.corporate_actions import build_realized_total_return_as_of
        from data.universe.calendar import session_close_cutoff

        with engine.connect() as conn:
            sim_corporate_actions = pd.read_sql(
                _text(
                    "SELECT ticker, ex_date, action_type, value, known_at, source_version "
                    "FROM corporate_actions ORDER BY ticker, ex_date"
                ),
                conn,
            )
        if sim_corporate_actions.empty:
            return today_closes, prev_day_closes

        sim_corporate_actions["ex_date"] = pd.to_datetime(sim_corporate_actions["ex_date"]).dt.date
        sim_corporate_actions["known_at"] = pd.to_datetime(
            sim_corporate_actions["known_at"], utc=True
        )
        adjusted_sim_prices, _adj_meta = build_realized_total_return_as_of(
            prices_df,
            sim_corporate_actions,
            entry_date=prev_date,
            exit_cutoff=session_close_cutoff(sim_date),
        )
        adj_lookup = adjusted_sim_prices.set_index(["ticker", "date"])["adj_close"].astype(float)
        return adj_lookup.xs(sim_date, level="date"), adj_lookup.xs(prev_date, level="date")
    except Exception as exc:
        import structlog as _structlog

        _structlog.get_logger("rqis.airflow").warning(
            "simulation_corporate_action_adjustment_unavailable",
            sim_date=str(sim_date),
            error=str(exc),
            note="close-to-close simulated_return falls back to RAW prices "
            "for this run; a split/dividend on sim_date or prev_date may "
            "distort the simulated return (BUG-009 section 2.3)",
        )
        return today_closes, prev_day_closes


def _write_simulation(**context: Any) -> dict:
    """Forward-simulate each strategy using today's alpha scores and daily_prices.

    For each strategy registered in the strategies table, takes the top-N alpha
    scores (N = 20 by default), computes a daily equal-weight portfolio return
    using the close-to-close return from daily_prices, and upserts one row into
    strategy_simulations.  Non-blocking: a missing price or score for a strategy
    is logged and skipped; it does not abort the pipeline.

    The simulated NAV chain starts at 1_000_000 and compounds daily.  Each day's
    row carries the NAV implied by the previous row in the table plus today's
    simulated return.
    """
    import json
    import os
    import uuid as _uuid
    from datetime import UTC, datetime as _datetime

    import pandas as pd
    from sqlalchemy import create_engine, text

    ti = context["ti"]
    score_date_str: str | None = ti.xcom_pull(key="score_date", task_ids="load_prices")
    alpha_json: str | None = ti.xcom_pull(key="alpha_scores_json", task_ids="combine_scores")
    prices_json: str | None = ti.xcom_pull(key="prices_json", task_ids="load_prices")

    if not score_date_str or not alpha_json or alpha_json == "[]":
        return {"simulations_written": 0, "reason": "no alpha scores"}
    if not prices_json:
        return {"simulations_written": 0, "reason": "no prices data"}

    sim_date = date.fromisoformat(score_date_str)
    alpha_df = pd.read_json(alpha_json, orient="records", convert_dates=False)
    if "score_date" in alpha_df.columns:
        alpha_df["score_date"] = pd.to_datetime(alpha_df["score_date"]).dt.date

    prices_df = pd.read_json(prices_json, orient="records", convert_dates=False)
    prices_df["date"] = pd.to_datetime(prices_df["date"]).dt.date

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return {"simulations_written": 0, "reason": "DATABASE_URL not set"}

    engine = create_engine(database_url)
    computed_at = _datetime.now(UTC).isoformat()
    written = 0

    try:
        with engine.connect() as conn:
            strategy_rows = conn.execute(
                text("SELECT strategy_id FROM strategies")
            ).fetchall()
        strategy_ids = [r[0] for r in strategy_rows]
        if not strategy_ids:
            strategy_ids = [_DEFAULT_STRATEGY_ID]
    except Exception:
        strategy_ids = [_DEFAULT_STRATEGY_ID]

    # BUG-009 section 4 (adversarial review round 3): research_run_id is now
    # part of alpha_scores' identity, so multiple rows can legitimately
    # coexist for the same (ticker, score_date, strategy_id) across runs
    # (legacy, superseded, active). The DB fallback query below (for
    # strategies not present in this run's own XCom) must filter to the
    # explicitly active run, never read across all of them — non-blocking,
    # consistent with this function's existing degrade-and-skip philosophy:
    # if no active run can be resolved, the DB fallback branch is skipped
    # (that strategy's simulation row is simply not written this run) rather
    # than aborting the whole task.
    try:
        _fallback_research_run_id: int | None = _get_active_research_run_id_sql(
            database_url, _OPERATIONAL_METHODOLOGY_NAME
        )
    except Exception as _exc:
        _fallback_research_run_id = None
        import structlog as _structlog

        _structlog.get_logger("rqis.airflow").warning(
            "simulation_fallback_active_run_unavailable",
            error=str(_exc),
            note="DB fallback alpha_scores lookup for other strategies will be "
            "skipped this run (BUG-009 section 4)",
        )

    # Compute close-to-close daily returns from prices for sim_date. See
    # _adjusted_closes_for_simulation's docstring (BUG-009 section 2.3,
    # adversarial-review round 10 self-audit sweep) for why this must be
    # corporate-action-adjusted, not raw.
    today_closes = prices_df[prices_df["date"] == sim_date].set_index("ticker")["close"]
    prev_day_closes: pd.Series = pd.Series(dtype=float)
    prior_dates = prices_df[prices_df["date"] < sim_date]["date"]
    if not prior_dates.empty:
        prev_date = prior_dates.max()
        prev_day_closes = (
            prices_df[prices_df["date"] == prev_date].set_index("ticker")["close"]
        )
        today_closes, prev_day_closes = _adjusted_closes_for_simulation(
            prices_df, engine, sim_date, prev_date, today_closes, prev_day_closes
        )

    for strategy_id in strategy_ids:
        try:
            # Use XCom scores for the current run's strategy; for other registered
            # strategies query the DB directly so every strategy gets a simulation
            # row on the same sim_date even when only one strategy ran today.
            if not alpha_df.empty and (alpha_df["strategy_id"] == strategy_id).any():
                strat_scores = alpha_df[alpha_df["strategy_id"] == strategy_id].copy()
            elif _fallback_research_run_id is not None:
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            "SELECT ticker, rank, alpha_score FROM alpha_scores "
                            "WHERE strategy_id = :sid AND score_date = :d "
                            "AND research_run_id = :run_id"
                        ),
                        {"sid": strategy_id, "d": sim_date, "run_id": _fallback_research_run_id},
                    ).fetchall()
                if not rows:
                    continue
                strat_scores = pd.DataFrame(rows, columns=["ticker", "rank", "alpha_score"])
            else:
                continue

            if strat_scores.empty:
                continue

            n_long = 20
            top_n = strat_scores.nsmallest(n_long, "rank")
            tickers = top_n["ticker"].tolist()
            weight = 1.0 / len(tickers) if tickers else 0.0
            target_weights = {t: weight for t in tickers}

            # Compute equal-weight portfolio return for sim_date.
            # Divide by len(tickers) — the number of selected positions — so the
            # weights sum to 100% regardless of whether the universe is smaller than
            # n_long.  Tickers with missing prior-day prices contribute 0% to the
            # average (cash-equivalent treatment).
            returns = []
            for ticker in tickers:
                if ticker in today_closes.index and ticker in prev_day_closes.index:
                    c_today = float(today_closes[ticker])
                    c_prev = float(prev_day_closes[ticker])
                    if c_prev > 0:
                        returns.append((c_today - c_prev) / c_prev)
            simulated_return = float(sum(returns) / len(tickers)) if returns else 0.0

            # Compound NAV from prior row, starting at 1_000_000
            with engine.connect() as conn:
                prior = conn.execute(
                    text(
                        "SELECT simulated_nav FROM strategy_simulations "
                        "WHERE strategy_id = :sid AND sim_date < :d "
                        "ORDER BY sim_date DESC LIMIT 1"
                    ),
                    {"sid": strategy_id, "d": sim_date},
                ).fetchone()
            prior_nav = float(prior[0]) if prior else 1_000_000.0
            simulated_nav = prior_nav * (1.0 + simulated_return)

            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO strategy_simulations
                            (id, strategy_id, sim_date, target_weights,
                             simulated_return, simulated_nav,
                             universe_size, n_positions, computed_at_utc)
                        VALUES
                            (:id, :strategy_id, :sim_date, CAST(:target_weights AS jsonb),
                             :simulated_return, :simulated_nav,
                             :universe_size, :n_positions, :computed_at_utc)
                        ON CONFLICT (strategy_id, sim_date)
                        DO UPDATE SET
                            target_weights    = EXCLUDED.target_weights,
                            simulated_return  = EXCLUDED.simulated_return,
                            simulated_nav     = EXCLUDED.simulated_nav,
                            universe_size     = EXCLUDED.universe_size,
                            n_positions       = EXCLUDED.n_positions,
                            computed_at_utc   = EXCLUDED.computed_at_utc
                    """),
                    {
                        "id": str(_uuid.uuid4()),
                        "strategy_id": strategy_id,
                        "sim_date": sim_date,
                        "target_weights": json.dumps(target_weights),
                        "simulated_return": round(simulated_return, 8),
                        "simulated_nav": round(simulated_nav, 6),
                        "universe_size": len(strat_scores),
                        "n_positions": len(tickers),
                        "computed_at_utc": computed_at,
                    },
                )
            written += 1
        except Exception as exc:
            import structlog as _sl
            _sl.get_logger("rqis.airflow").warning(
                "strategy_simulation_failed",
                strategy_id=strategy_id,
                sim_date=str(sim_date),
                error=str(exc),
            )

    engine.dispose()
    return {"simulations_written": written}


_OPERATIONAL_METHODOLOGY_NAME = "daily_signal_pipeline_operational"


def _get_active_research_run_id_sql(database_url: str, methodology_name: str) -> int:
    """Resolve the id of the explicitly active research_runs row for
    ``methodology_name`` via plain SQL, or raise.

    SQLAlchemy-1.4-compatible ON PURPOSE, same reason as
    ``_pit_eligible_tickers_sql`` above: the packaged Airflow runtime image
    pins SQLAlchemy 1.4.51 (``infra/docker/Dockerfile.airflow``), while
    ``data.research.models``/``data.research.identity`` use SQLAlchemy-2-only
    APIs (``DeclarativeBase``/``Mapped``/``mapped_column``). Importing them
    here — even lazily, inside a task function — would raise ``ImportError``
    the moment this task actually executes inside that image, not at DAG
    parse time.

    Delegates to ``data.research.sql_compat.get_active_research_run_id`` —
    a shared, plain-``text()``-only implementation (round 5 of the 01B-3 PR's
    adversarial review found the identical SQLAlchemy-1.4 incompatibility
    reintroduced at a second call site, ``scripts/paper_inputs_check.py``,
    because that fix reused the ORM path instead of this one; centralizing
    the lookup is meant to stop that recurring a third time). That module
    has no ORM imports of its own, so importing it here is safe — the same
    way ``data.normalization.corporate_actions``/``data.universe.calendar``
    are already imported elsewhere in this DAG; the restriction is on the
    SQLAlchemy-2-only ORM modules specifically, not the whole ``data``
    package.
    """
    from data.research.sql_compat import get_active_research_run_id

    return get_active_research_run_id(database_url, methodology_name)


def _write_scores(**context: Any) -> dict:
    """Persist factor_scores and alpha_scores to TimescaleDB.

    BUG-009 section 4 / migration 012: research_run_id is now part of the
    unique constraint/primary key on both tables, so every row must be
    tagged with an explicitly approved active research run — never assumed.
    Fails closed with an actionable error if no run has been activated for
    ``_OPERATIONAL_METHODOLOGY_NAME`` (an operator runs
    ``python -m scripts.register_operational_research_run`` once — see
    ``docs/runbooks/research_run_registration.md`` — a REQUIRED pre-deploy
    step after migration 012; this DAG never registers or activates a run
    itself).

    Adversarial-review round 11 (BUG-008/BUG-009 section 4): this DAG's own
    degrade path had never been routed through a methodology-honesty check
    (the ad hoc checks built in rounds 9-10 only ever covered standalone
    scripts). ``_OPERATIONAL_METHODOLOGY_NAME``'s registered methodology
    claims PIT-universe safety (``universe_import_policy =
    "pit_universe_effective_dated_v1"``), but ``_load_prices``/
    ``_compute_quality`` degrade to an unfiltered, provisional cross-section
    (BUG-069) on a PIT lookup failure with only a log warning -- silently
    tagging those degraded rows with the PIT-claiming operational run would
    misrepresent what was actually computed. This now fails closed via the
    single shared
    ``data.research.sql_compat.assert_methodology_write_is_honest`` gate
    before any row is written, superseding BUG-069's "silently write
    provisional scores" acceptance for the WRITE path specifically (the
    task now fails loudly instead) -- an operator who wants degraded days to
    keep persisting must register and activate a run under a distinct
    methodology that honestly declares no PIT filtering, matching the same
    pattern used everywhere else in this PR (never done automatically).
    Corporate-action adjustment has no equivalent degrade path here:
    ``_load_prices`` has no try/except around its corporate_actions query,
    so a failure there fails the task outright rather than silently
    persisting raw prices -- the operational methodology's
    ``score_cutoff_known_at_v1`` claim is always true for whatever this DAG
    actually persists.
    """
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

    database_url = os.environ["DATABASE_URL"]
    research_run_id = _get_active_research_run_id_sql(database_url, _OPERATIONAL_METHODOLOGY_NAME)

    # PIT-universe honesty gate (see docstring above). Two independent PIT
    # lookups feed this write: _load_prices' panel filter (covers momentum/
    # lowvol/value) and _compute_quality's own separate lookup (quality
    # only). Missing XCom (task didn't run / older DAG run) is treated
    # conservatively as "not applied" -- fail safe, never assume success.
    load_prices_pit_applied = bool(
        context["ti"].xcom_pull(key="pit_universe_applied", task_ids="load_prices")
    )
    quality_pit_applied_raw = context["ti"].xcom_pull(
        key="quality_pit_universe_applied", task_ids="compute_quality"
    )
    quality_pit_applied = True if quality_pit_applied_raw is None else bool(quality_pit_applied_raw)
    pit_universe_applied = load_prices_pit_applied and quality_pit_applied

    if not pit_universe_applied:
        from data.research.sql_compat import assert_methodology_write_is_honest

        assert_methodology_write_is_honest(
            database_url,
            research_run_id,
            pit_universe_applied=False,
            corporate_action_adjustment_applied=True,
        )

    engine = create_engine(database_url)

    factor_count = 0
    alpha_count = 0

    # Single transaction: both tables commit together or neither does
    with engine.begin() as conn:
        if not factor_df.empty:
            factor_records = factor_df.to_dict("records")
            for record in factor_records:
                record["research_run_id"] = research_run_id
            conn.execute(
                text(
                    "INSERT INTO factor_scores "
                    "(ticker, score_date, factor_name, strategy_id, research_run_id, z_score, raw_value) "
                    "VALUES (:ticker, :score_date, :factor_name, :strategy_id, "
                    ":research_run_id, :z_score, :raw_value) "
                    "ON CONFLICT (ticker, score_date, factor_name, strategy_id, research_run_id) "
                    "DO UPDATE SET z_score = EXCLUDED.z_score, "
                    "raw_value = EXCLUDED.raw_value, "
                    "computed_at = NOW()"
                ),
                factor_records,
            )
            factor_count = len(factor_records)

        if not alpha_df.empty:
            alpha_records = alpha_df.to_dict("records")
            for record in alpha_records:
                record["research_run_id"] = research_run_id
            conn.execute(
                text(
                    "INSERT INTO alpha_scores "
                    "(ticker, score_date, strategy_id, research_run_id, alpha_score, rank, universe_size) "
                    "VALUES (:ticker, :score_date, :strategy_id, :research_run_id, "
                    ":alpha_score, :rank, :universe_size) "
                    "ON CONFLICT (ticker, score_date, strategy_id, research_run_id) "
                    "DO UPDATE SET alpha_score = EXCLUDED.alpha_score, "
                    "rank = EXCLUDED.rank, "
                    "universe_size = EXCLUDED.universe_size, "
                    "computed_at = NOW()"
                ),
                alpha_records,
            )
            alpha_count = len(alpha_records)

    return {"factor_rows": factor_count, "alpha_rows": alpha_count, "research_run_id": research_run_id}


# ─── DAG definition ──────────────────────────────────────────────────────────

with DAG(
    dag_id="daily_signal_pipeline",
    default_args=_default_args,
    description="Daily factor scoring and composite alpha computation",
    schedule_interval="30 21 * * 1-5",   # 21:30 ET weekdays (01:30 UTC in EDT / 02:30 UTC in EST)
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

    t_simulate = PythonOperator(
        task_id="write_simulations",
        python_callable=_write_simulation,
        trigger_rule="none_failed",
    )

    # ── Task dependency graph ─────────────────────────────────────────────────
    t_load_prices >> [t_momentum, t_lowvol, t_value, t_quality]
    [t_momentum, t_lowvol, t_value, t_quality] >> t_combine
    t_combine >> t_write >> t_simulate
