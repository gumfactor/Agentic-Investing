"""IC validation engine for signal research.

Computes Information Coefficient (IC) and related diagnostics for any factor
that produces cross-sectional scores.  Results can be persisted to MLflow
and the signal_ic_stats DB table.

Survivorship-bias note
----------------------
Phase 1 uses a current-membership S&P 500 universe.  All IC results computed
against that universe are labelled provisional until point-in-time constituent
history replaces it in Phase 2/3.  Pass ``data_version`` to every MLflow call
so results are traceable to the snapshot used (C7).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats
import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_HORIZONS: list[int] = [1, 5, 10, 21, 63]
_MIN_TICKERS_PER_DATE = 5     # discard IC obs where universe shrinks below this
_MIN_IC_DATES_FOR_TSTAT = 10  # need this many IC obs to report a t-stat


# ─── Forward returns ──────────────────────────────────────────────────────────

def compute_forward_returns(
    prices: pd.DataFrame,
    horizons: list[int],
) -> pd.DataFrame:
    """Compute forward returns at multiple horizons.

    Args:
        prices: Long-format DataFrame with columns ``ticker``, ``date``,
            ``close``.
        horizons: Forward return horizons in trading days (e.g. [1, 5, 21]).

    Returns:
        Long-format DataFrame with columns:
            ``ticker``, ``date``, ``horizon_days``, ``forward_return``

        The ``date`` column is the *signal date* — the close that starts the
        return window.  Rows where the horizon extends beyond available data
        are dropped (no NaN forward returns).
    """
    _validate_prices(prices)

    wide = (
        prices[["ticker", "date", "close"]]
        .assign(close=lambda df: df["close"].astype(float))
        .pivot_table(index="date", columns="ticker", values="close")
        .sort_index()
    )
    wide.columns.name = None

    frames: list[pd.DataFrame] = []
    for h in horizons:
        fwd = wide.shift(-h) / wide - 1.0
        melted = (
            fwd.reset_index()
            .melt(id_vars="date", var_name="ticker", value_name="forward_return")
            .dropna(subset=["forward_return"])
        )
        melted["horizon_days"] = h
        frames.append(melted)

    if not frames:
        return pd.DataFrame(columns=["ticker", "date", "horizon_days", "forward_return"])

    return pd.concat(frames, ignore_index=True)


# ─── IC series ────────────────────────────────────────────────────────────────

def compute_ic_series(
    scores: pd.DataFrame,
    prices: pd.DataFrame,
    score_col: str,
    horizons: Optional[list[int]] = None,
) -> pd.DataFrame:
    """Compute per-date cross-sectional IC at multiple forward return horizons.

    Cross-sectional IC on date *t* is the correlation between tickers' scores
    at *t* and their *h*-day forward returns starting from *t*.

    Args:
        scores: Long-format DataFrame with columns ``ticker``, ``date``,
            and ``score_col``.
        prices: Long-format DataFrame with columns ``ticker``, ``date``,
            ``close``.
        score_col: Column in *scores* to evaluate.
        horizons: Forward return horizons in trading days.
            Defaults to ``[1, 5, 10, 21, 63]``.

    Returns:
        DataFrame with columns:
            ``date``, ``horizon_days``, ``ic``, ``rank_ic``, ``n_obs``

        One row per (date, horizon) pair where at least
        ``_MIN_TICKERS_PER_DATE`` tickers had valid scores and forward
        returns.  Rows where fewer tickers are available are dropped.
    """
    if horizons is None:
        horizons = _DEFAULT_HORIZONS

    _validate_scores(scores, score_col)
    _validate_prices(prices)

    fwd = compute_forward_returns(prices, horizons)

    merged = scores[["ticker", "date", score_col]].merge(
        fwd, on=["ticker", "date"], how="inner"
    )

    rows: list[dict] = []
    for (dt, h), group in merged.groupby(["date", "horizon_days"]):
        valid = group.dropna(subset=[score_col, "forward_return"])
        n = len(valid)
        if n < _MIN_TICKERS_PER_DATE:
            continue

        ic_val = float(
            valid[score_col].corr(valid["forward_return"], method="pearson")
        )
        rank_ic_val = float(
            valid[score_col].corr(valid["forward_return"], method="spearman")
        )

        rows.append({
            "date": dt,
            "horizon_days": int(h),
            "ic": ic_val,
            "rank_ic": rank_ic_val,
            "n_obs": n,
        })

    if not rows:
        return pd.DataFrame(columns=["date", "horizon_days", "ic", "rank_ic", "n_obs"])

    result = (
        pd.DataFrame(rows)
        .sort_values(["horizon_days", "date"])
        .reset_index(drop=True)
    )

    logger.info(
        "ic_series_computed",
        score_col=score_col,
        horizons=horizons,
        n_date_horizon_pairs=len(result),
        ic_mean={
            h: round(float(g["ic"].mean()), 4)
            for h, g in result.groupby("horizon_days")
        },
    )
    return result


# ─── IC summary ───────────────────────────────────────────────────────────────

def summarize_ic(
    ic_series: pd.DataFrame,
    factor_name: str,
    strategy_id: str = "default",
    eval_date: Optional[date] = None,
) -> pd.DataFrame:
    """Aggregate an IC series into per-horizon summary statistics.

    Args:
        ic_series: Output of :func:`compute_ic_series`.
        factor_name: Factor identifier (e.g. ``'momentum_composite'``).
        strategy_id: Strategy config version identifier.
        eval_date: Last date of the evaluation window.  Defaults to the
            maximum date present in *ic_series*.

    Returns:
        DataFrame with columns:
            ``factor_name``, ``strategy_id``, ``eval_date``,
            ``horizon_days``, ``ic``, ``rank_ic``, ``ic_tstat``,
            ``ic_ir``, ``ic_pvalue``, ``n_observations``
        One row per horizon.  Horizons with fewer than
        ``_MIN_IC_DATES_FOR_TSTAT`` observations are excluded.
    """
    _COLS = [
        "factor_name", "strategy_id", "eval_date", "horizon_days",
        "ic", "rank_ic", "ic_tstat", "ic_ir", "ic_pvalue", "n_observations",
    ]
    if ic_series.empty:
        return pd.DataFrame(columns=_COLS)

    if eval_date is None:
        eval_date = ic_series["date"].max()

    rows: list[dict] = []
    for h, group in ic_series.groupby("horizon_days"):
        ic_vals = group["ic"].dropna()
        rank_ic_vals = group["rank_ic"].dropna()
        n_dates = len(ic_vals)

        if n_dates < _MIN_IC_DATES_FOR_TSTAT:
            continue

        ic_mean = float(ic_vals.mean())
        ic_std = float(ic_vals.std(ddof=1))
        rank_ic_mean = float(rank_ic_vals.mean()) if len(rank_ic_vals) >= 2 else float("nan")

        tstat, pvalue = stats.ttest_1samp(ic_vals.values, popmean=0.0)
        ic_ir = ic_mean / ic_std if ic_std > 0 else float("nan")

        rows.append({
            "factor_name": factor_name,
            "strategy_id": strategy_id,
            "eval_date": eval_date,
            "horizon_days": int(h),
            "ic": round(ic_mean, 6),
            "rank_ic": round(rank_ic_mean, 6),
            "ic_tstat": round(float(tstat), 6),
            "ic_ir": round(ic_ir, 6) if not np.isnan(ic_ir) else float("nan"),
            "ic_pvalue": round(float(pvalue), 6),
            "n_observations": n_dates,
        })

    return pd.DataFrame(rows, columns=_COLS)


# ─── Multiple testing correction ─────────────────────────────────────────────

def multiple_testing_correction(
    summaries: pd.DataFrame,
    method: str = "bhy",
    alpha: float = 0.05,
) -> pd.DataFrame:
    """Apply multiple testing correction to a collection of factor IC p-values.

    Use this when evaluating several factors simultaneously to control the
    false discovery rate (FDR).  Each row in *summaries* (typically one
    per factor × horizon) is treated as one hypothesis test.

    Args:
        summaries: DataFrame containing an ``ic_pvalue`` column.
            Typically the concatenated output of :func:`summarize_ic` across
            multiple factors.
        method: ``'bhy'`` (Benjamini-Hochberg-Yekutieli, the default — valid
            under arbitrary dependence between tests) or ``'bh'``
            (Benjamini-Hochberg — valid under independence / positive
            dependence).
        alpha: Target FDR level.  Default ``0.05``.

    Returns:
        Input DataFrame with two additional columns:
            ``corrected_pvalue`` — BH/BHY-adjusted p-value (the effective
            threshold, not a transformed p-value).
            ``significant`` — bool, True if the test survives correction.
    """
    if "ic_pvalue" not in summaries.columns:
        raise ValueError("summaries must contain an 'ic_pvalue' column")

    from statsmodels.stats.multitest import multipletests  # local import

    _method_map = {"bh": "fdr_bh", "bhy": "fdr_by"}
    if method not in _method_map:
        raise ValueError(f"method must be 'bh' or 'bhy', got {method!r}")

    out = summaries.copy()
    pvals = out["ic_pvalue"].fillna(1.0).values

    reject, corrected_p, _, _ = multipletests(pvals, alpha=alpha, method=_method_map[method])

    out["corrected_pvalue"] = corrected_p
    out["significant"] = reject
    return out


# ─── Train / validation split ─────────────────────────────────────────────────

def chronological_split(
    scores: pd.DataFrame,
    train_fraction: float = 0.7,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a scores DataFrame into chronological train and validation sets.

    Splits on unique dates so no date appears in both sets.

    Args:
        scores: DataFrame with a ``date`` column.
        train_fraction: Fraction of dates to include in the training set.

    Returns:
        ``(train, validation)`` tuple of DataFrames.
    """
    dates = sorted(scores["date"].unique())
    n_train = max(1, int(len(dates) * train_fraction))
    train_dates = set(dates[:n_train])
    train = scores[scores["date"].isin(train_dates)].reset_index(drop=True)
    val = scores[~scores["date"].isin(train_dates)].reset_index(drop=True)
    return train, val


# ─── MLflow logging ───────────────────────────────────────────────────────────

def log_ic_to_mlflow(
    summary: pd.DataFrame,
    factor_name: str,
    strategy_id: str,
    data_version: str,
    experiment_name: Optional[str] = None,
) -> Optional[str]:
    """Log IC summary statistics to an MLflow experiment.

    Args:
        summary: Output of :func:`summarize_ic` for a single factor.
        factor_name: Factor identifier (used for run naming and tags).
        strategy_id: Strategy config version.
        data_version: Snapshot version of the input data (C7 — required).
            Obtain via ``scripts/pin_snapshot.py``.
        experiment_name: MLflow experiment name.  Defaults to
            ``signals/{factor_name}``.

    Returns:
        MLflow run ID string, or ``None`` if MLflow is unavailable.
    """
    if not data_version:
        raise ValueError(
            "data_version is required (C7: every signal run must record the "
            "data snapshot version in MLflow). Run scripts/pin_snapshot.py first."
        )

    try:
        import mlflow  # local import — not a hard dep at module load time
    except ImportError:
        logger.warning("mlflow_unavailable", msg="pip install mlflow to enable experiment tracking")
        return None

    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)

    exp_name = experiment_name or f"signals/{factor_name}"
    mlflow.set_experiment(exp_name)

    try:
        with mlflow.start_run(run_name=f"{factor_name}_{strategy_id}") as run:
            mlflow.set_tags({
                "factor_name": factor_name,
                "strategy_id": strategy_id,
                "data_version": data_version,
                "survivorship_bias_note": (
                    "provisional — current-membership S&P 500 universe; "
                    "replace with PIT constituents before Phase 3"
                ),
            })
            for _, row in summary.iterrows():
                h = int(row["horizon_days"])
                prefix = f"h{h:03d}"
                for col in ["ic", "rank_ic", "ic_tstat", "ic_ir", "ic_pvalue", "n_observations"]:
                    val = row.get(col)
                    if val is not None and not (isinstance(val, float) and np.isnan(val)):
                        mlflow.log_metric(f"{prefix}_{col}", float(val))

            run_id = run.info.run_id

        logger.info("ic_logged_to_mlflow", run_id=run_id, experiment=exp_name)
        return run_id

    except Exception as exc:
        logger.warning("mlflow_log_failed", error=str(exc))
        return None


# ─── Input validation ─────────────────────────────────────────────────────────

def _validate_scores(scores: pd.DataFrame, score_col: str) -> None:
    required = {"ticker", "date", score_col}
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"scores DataFrame missing columns: {missing}")
    if scores.empty:
        raise ValueError("scores DataFrame is empty")


def _validate_prices(prices: pd.DataFrame) -> None:
    required = {"ticker", "date", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices DataFrame missing columns: {missing}")
    if prices.empty:
        raise ValueError("prices DataFrame is empty")
