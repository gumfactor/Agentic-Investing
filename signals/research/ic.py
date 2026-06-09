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

import bisect
import os
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats
import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_HORIZONS: list[int] = [1, 5, 10, 21, 63]
_MIN_TICKERS_PER_DATE = 5   # discard IC obs where universe shrinks below this
# 30 is the practical floor for a reliable t-stat; IC time series are
# autocorrelated, so effective sample size is lower than the raw count.
_MIN_IC_DATES_FOR_TSTAT = 30


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

    # shift(-h) advances by h *rows* in the wide matrix, not h calendar days.
    # For a well-formed daily universe (e.g. S&P 500 with data on every
    # trading day), rows correspond 1-to-1 with trading days so the result
    # is correct.  Tickers with missing dates produce NaN forward returns
    # (those rows are dropped later) — no bias is introduced, but the sample
    # shrinks.  If your universe has irregular coverage, validate separately.
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
        # rank_ic may have a different valid count from ic; n_observations
        # tracks IC dates only (used for the t-stat denominator).
        rank_ic_mean = float(rank_ic_vals.mean()) if len(rank_ic_vals) >= 2 else float("nan")

        # One-sided t-test: H0: mean_IC = 0, H1: mean_IC > 0.
        # Factors with consistently negative IC are reversible; the researcher
        # should re-sign those scores rather than using a two-sided test.
        tstat, pvalue = stats.ttest_1samp(ic_vals.values, popmean=0.0, alternative="greater")
        # IC-IR = mean / std of the IC time series (unannualized Sharpe of IC).
        # Not scaled by sqrt(periods_per_year) — comparisons across horizons
        # should account for the different sampling frequencies.
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
    if scores.empty:
        return scores.copy(), scores.copy()
    if not 0.0 < train_fraction <= 1.0:
        raise ValueError(f"train_fraction must be in (0, 1], got {train_fraction}")
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


# ─── Factor turnover ─────────────────────────────────────────────────────────

def compute_factor_turnover(
    scores: pd.DataFrame,
    score_col: str,
    rebalance_days: int = 21,
) -> pd.DataFrame:
    """Compute factor rank autocorrelation at a given rebalancing lag.

    For each date *t* in the score series, finds the most-recent prior date
    that is at least ``rebalance_days`` calendar days before *t*, then
    computes the Spearman rank correlation between the two cross-sections.

    High autocorrelation = low turnover = lower implied transaction costs.
    A rank autocorrelation of 0.95 at a 21-day lag means roughly 5 % of
    relative rankings change each month — equivalent to low portfolio
    churn.

    Args:
        scores: Long-format DataFrame with columns ``ticker``, ``date``,
            and ``score_col``.
        score_col: Column name for the factor score.
        rebalance_days: Minimum calendar-day gap between the two
            cross-sections used for autocorrelation.  Default 21 ≈ 1 month.

    Returns:
        DataFrame with columns:
            ``score_date``, ``lag_date``, ``rank_autocorrelation``,
            ``ticker_count``

        Dates with fewer than ``_MIN_TICKERS_PER_DATE`` tickers on
        either leg are excluded.
    """
    _validate_scores(scores, score_col)

    wide = (
        scores[["ticker", "date", score_col]]
        .pivot_table(index="date", columns="ticker", values=score_col)
    )
    all_dates = sorted(wide.index)

    rows: list[dict] = []
    for i, curr_date in enumerate(all_dates):
        target_prior = curr_date - timedelta(days=rebalance_days)
        # O(log N) search for the nearest date ≤ target_prior
        idx = bisect.bisect_right(all_dates, target_prior) - 1
        if idx < 0:
            continue
        prior_date = all_dates[idx]

        curr_row = wide.loc[curr_date].dropna()
        prior_row = wide.loc[prior_date].dropna()
        common = curr_row.index.intersection(prior_row.index)
        if len(common) < _MIN_TICKERS_PER_DATE:
            continue

        rho = float(curr_row[common].rank().corr(prior_row[common].rank()))
        rows.append({
            "score_date": curr_date,
            "lag_date": prior_date,
            "lag_calendar_days": (curr_date - prior_date).days,
            "rank_autocorrelation": rho,
            "ticker_count": len(common),
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["score_date", "lag_date", "lag_calendar_days",
                 "rank_autocorrelation", "ticker_count"]
    )


# ─── Rolling IC (walk-forward stability) ─────────────────────────────────────

def rolling_ic_summary(
    ic_series: pd.DataFrame,
    trailing_dates: int = 252,
    min_dates: int = _MIN_IC_DATES_FOR_TSTAT,
) -> pd.DataFrame:
    """Compute rolling IC statistics over a trailing window of dates.

    For each date in *ic_series*, aggregates IC over the preceding
    ``trailing_dates`` score dates (not calendar days).  Useful for
    detecting IC decay, regime changes, and factor instability before
    committing to a live portfolio.

    Note on ``trailing_dates``: this is a count of IC observation rows,
    not calendar days.  If the IC series is sparse (dates dropped due to
    insufficient universe size), a window of 252 may span more than one
    calendar year.  Pre-filter ``ic_series`` to a specific date range to
    enforce a calendar-day cap.

    Args:
        ic_series: Output of :func:`compute_ic_series` — long-format
            DataFrame with columns ``date``, ``horizon_days``, ``ic``,
            ``rank_ic``.
        trailing_dates: Number of preceding dates to include in each
            rolling window.  Default 252 ≈ 1 trading year.
        min_dates: Minimum number of dates required to compute a summary.
            Windows shorter than this are excluded.

    Returns:
        DataFrame with columns:
            ``score_date``, ``horizon_days``, ``ic_mean``, ``rank_ic_mean``,
            ``ic_std``, ``ic_ir``, ``hit_rate``, ``n_dates``

        One row per (date, horizon) where the trailing window is long
        enough.  ``score_date`` is the last date in the window.
    """
    required = {"date", "horizon_days", "ic", "rank_ic"}
    missing = required - set(ic_series.columns)
    if missing:
        raise ValueError(f"ic_series missing columns: {missing}")
    if ic_series.empty:
        return pd.DataFrame(
            columns=["score_date", "horizon_days", "ic_mean", "rank_ic_mean",
                     "ic_std", "ic_ir", "hit_rate", "n_dates"]
        )

    rows: list[dict] = []

    for horizon in sorted(ic_series["horizon_days"].unique()):
        sub = (
            ic_series[ic_series["horizon_days"] == horizon]
            .sort_values("date")
            .reset_index(drop=True)
        )
        ics = sub["ic"].values
        rank_ics = sub["rank_ic"].values
        dates = sub["date"].values

        for i in range(len(dates)):
            start_idx = max(0, i - trailing_dates + 1)
            window_ic = ics[start_idx : i + 1]
            window_rank_ic = rank_ics[start_idx : i + 1]

            n = len(window_ic)
            if n < min_dates:
                continue

            ic_mean = float(np.nanmean(window_ic))
            rank_ic_mean = float(np.nanmean(window_rank_ic))
            ic_std = float(np.nanstd(window_ic, ddof=1)) if n > 1 else np.nan
            ic_ir = ic_mean / ic_std if (ic_std and not np.isnan(ic_std) and ic_std > 0) else np.nan
            hit_rate = float(np.mean(window_ic > 0))

            rows.append({
                "score_date": dates[i],
                "horizon_days": int(horizon),
                "ic_mean": ic_mean,
                "rank_ic_mean": rank_ic_mean,
                "ic_std": ic_std,
                "ic_ir": ic_ir,
                "hit_rate": hit_rate,
                "n_dates": n,
            })

    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["score_date", "horizon_days", "ic_mean", "rank_ic_mean",
                 "ic_std", "ic_ir", "hit_rate", "n_dates"]
    )


# ─── IC-IR weighted blend weights ────────────────────────────────────────────

def compute_ic_ir_weights(
    ic_summaries: dict[str, pd.DataFrame],
    horizon_days: int = 21,
    shrinkage: float = 0.25,
    min_ic_ir: float = 0.0,
    max_weight: float = 0.5,
) -> dict[str, float]:
    """Compute IC-IR-weighted blend weights for a set of factors.

    Produces weights suitable for passing to
    :func:`~signals.scoring.scorer.combine_factor_scores` ``weights``
    parameter.  This is the stretch-goal successor to equal-weight blending
    and should only be used after equal-weight IC has been validated.

    Design choices (following Grinold & Kahn "Active Portfolio Management"):
    - Negative IC-IR factors receive zero weight (cannot short a signal).
    - Weights are shrunk toward equal-weight to reduce estimation error.
    - No single factor may exceed ``max_weight`` after normalisation.

    Args:
        ic_summaries: dict mapping factor_name → output of
            :func:`~signals.research.ic.summarize_ic`.
        horizon_days: Which horizon row to extract IC-IR from.
        shrinkage: Fraction of weight pulled toward equal-weight.
            0 = pure IC-IR weighting; 1 = equal-weight.
        min_ic_ir: IC-IR values below this floor are treated as 0.
        max_weight: Hard cap on any single factor's normalised weight.

    Returns:
        dict of factor_name → weight, normalised to sum to 1.
        Returns equal-weight if no factor has positive IC-IR.

    Raises:
        ValueError: if ``shrinkage`` is outside [0, 1], ``min_ic_ir`` is
            negative, or ``max_weight`` is infeasible for the number of factors.
    """
    if not (0.0 <= shrinkage <= 1.0):
        raise ValueError(f"shrinkage must be in [0, 1], got {shrinkage}")
    if min_ic_ir < 0.0:
        raise ValueError(f"min_ic_ir must be >= 0 to prevent negative weights, got {min_ic_ir}")
    if not ic_summaries:
        return {}

    n_factors = len(ic_summaries)
    if max_weight * n_factors < 1.0 - 1e-9:
        raise ValueError(
            f"max_weight={max_weight} is infeasible with {n_factors} factors: "
            f"max possible weight sum = {max_weight * n_factors:.4f} < 1.0"
        )
    equal_w = 1.0 / n_factors

    raw_ic_ir: dict[str, float] = {}
    for name, summary in ic_summaries.items():
        matching = summary[summary["horizon_days"] == horizon_days]
        if matching.empty or "ic_ir" not in matching.columns:
            raw_ic_ir[name] = 0.0
        else:
            val = float(matching["ic_ir"].iloc[0])
            raw_ic_ir[name] = max(min_ic_ir, val) if not np.isnan(val) else 0.0

    total_ic_ir = sum(raw_ic_ir.values())
    if total_ic_ir <= 0:
        logger.warning("ic_ir_weights_all_zero_fallback_equal", factors=list(ic_summaries))
        return {name: equal_w for name in ic_summaries}

    # Pure IC-IR weights (normalised)
    pure_weights = {name: v / total_ic_ir for name, v in raw_ic_ir.items()}

    # Shrink toward equal-weight
    shrunk = {
        name: (1.0 - shrinkage) * pure_weights[name] + shrinkage * equal_w
        for name in ic_summaries
    }

    # Cap at max_weight: redistribute excess to positive-weight uncapped factors.
    # Zero-weight factors (IC-IR floored to 0) are excluded from redistribution
    # so a negative-IC-IR factor never receives weight from a capped positive factor.
    # Use a snapshot of pre-loop weights to avoid stale-denominator errors when
    # multiple uncapped factors are updated in the same iteration.
    normalised = dict(shrunk)
    for _ in range(100):
        over = {n: w for n, w in normalised.items() if w > max_weight + 1e-12}
        if not over:
            break
        excess = sum(w - max_weight for w in over.values())
        for n in over:
            normalised[n] = max_weight
        # Only redistribute to factors already carrying positive weight;
        # include factors sitting at max_weight (w > 1e-12 covers them)
        under = {n: w for n, w in normalised.items() if w > 1e-12 and n not in over}
        if not under:
            # No eligible recipients — normalise (zeros stay zero)
            total = sum(normalised.values())
            if total > 0:
                normalised = {n: w / total for n, w in normalised.items()}
            break
        # Snapshot weights before mutation to avoid stale-denominator in loop
        snapshot = dict(under)
        total_snapshot = sum(snapshot.values())
        for n, snap_w in snapshot.items():
            normalised[n] += excess * (snap_w / total_snapshot)
        total = sum(normalised.values())
        normalised = {n: w / total for n, w in normalised.items()}

    logger.info(
        "ic_ir_weights_computed",
        horizon_days=horizon_days,
        shrinkage=shrinkage,
        weights={n: round(v, 4) for n, v in normalised.items()},
    )
    return normalised


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
