"""IC validation engine for signal research.

Computes Information Coefficient (IC) and related diagnostics for any factor
that produces cross-sectional scores.  Results can be persisted to MLflow
and the signal_ic_stats DB table.

Survivorship-bias note (BUG-008 / 01B-2)
----------------------------------------
Point-in-time membership enforcement is available: pass a
``data.universe.runtime.PITUniverseLookup`` as the ``universe`` argument of
:func:`compute_ic_series`. With a universe, tickers that have prices but were
not (knowably) index members on the score date are excluded, out-of-coverage
dates fail closed, and an insufficient post-membership cross-section raises
instead of silently emitting IC from a shrunken universe. Current-universe
objects and plain ticker lists are rejected at the type level.

Calling without ``universe`` retains the legacy current-membership behavior;
those results remain **provisional** (design plan §1: they may be kept for
traceability but cannot be used for selection, promotion, or paper-trading
qualification). Pass ``data_version`` to every MLflow call so results are
traceable to the snapshot used (C7).

Timing contract (BUG-009 / 01B-3)
----------------------------------
``compute_forward_returns`` no longer computes a same-close return
(``close[t+h] / close[t] - 1``, using the signal date's own close). It
delegates to :func:`signals.research.timing.build_return_series`, which
enforces the design plan §2.1 baseline: a score observed at session t's
close cannot receive a return from before session t+1's close
(``score_date < entry_date`` is mandatory), and each ticker's own trading
calendar — not a shared wide-matrix row shift — determines its horizon.
Output rows are named ``score_date`` / ``entry_date`` / ``exit_date``
explicitly rather than a bare ``date``. Pass ``timing_policy`` to use a
different (still-approved) execution-lag contract; the policy identifier is
carried through to ``compute_ic_series`` output and downstream persistence.
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
from statsmodels.regression.linear_model import OLS

from signals.research.timing import (
    DEFAULT_TIMING_POLICY,
    RETURN_SERIES_COLUMNS,
    TimingPolicy,
    build_return_series,
)

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
    timing_policy: TimingPolicy = DEFAULT_TIMING_POLICY,
) -> pd.DataFrame:
    """Compute forward returns at multiple horizons under an explicit timing policy.

    BUG-009 fix: this used to compute a same-close return
    (``close[t+h] / close[t] - 1``), crediting the signal date's own close
    to the return — a one-bar lookahead when the signal itself can use that
    same close. It now delegates to
    :func:`signals.research.timing.build_return_series`, which enforces
    ``score_date < entry_date`` and names every date explicitly.

    Args:
        prices: Long-format DataFrame with columns ``ticker``, ``date``,
            ``close``. Use a total-return-adjusted close for research
            (design plan §2.2); this function performs no adjustment.
        horizons: Forward return horizons in trading *sessions* (e.g.
            [1, 5, 21]), each >= 1, measured from ``entry_date``.
        timing_policy: score_date -> entry_date execution-lag contract.
            Defaults to the baseline t+1 convention.

    Returns:
        Long-format DataFrame with columns ``ticker``, ``score_date``,
        ``entry_date``, ``exit_date``, ``horizon_days``, ``forward_return``,
        ``timing_policy_id``. Rows where the horizon extends beyond a given
        ticker's own available price history are dropped (no fabricated
        NaN forward returns); a holiday/missing bar for one ticker cannot
        shift another ticker's horizon because horizons are computed on
        each ticker's own date index, not a shared wide-matrix row shift.
    """
    _validate_prices(prices)
    return build_return_series(prices, horizons, timing_policy=timing_policy)


def compute_realized_forward_returns_as_of(
    prices: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    horizons: list[int],
    timing_policy: TimingPolicy = DEFAULT_TIMING_POLICY,
) -> pd.DataFrame:
    """Forward/realized returns using each row's OWN exit_date as the
    corporate-action knowledge cutoff — not one shared boundary cutoff.

    Adversarial-review round 4 finding (BUG-009 §2.3/§2.4): a single global
    boundary cutoff shared across every row (as an earlier draft of
    ``scripts/validate_signal_ic.py`` used for both the score and
    realized-return series) is safe for the SCORE series' ratio-cancellation
    argument (BUG-071 — a uniform adjustment factor across an entire
    lookback window ending at the score date cancels out of the ratio), but
    it is NOT safe for realized returns: for an earlier exit_date in a
    holdout, an action whose ``ex_date`` falls between that specific
    entry/exit pair but whose ``known_at`` is after that particular exit
    (yet still before the later shared boundary) would be incorrectly
    included — future information leaking into a "PIT-safe" realized
    return. This function eliminates that approximation entirely: for each
    DISTINCT ``exit_date`` needed, it builds a separate
    ``build_realized_total_return_as_of`` series with
    ``exit_cutoff = session_close_cutoff(exit_date)`` (that date's own
    cutoff, never a shared one), then reads each row's entry/exit adjusted
    closes from the series built for ITS OWN exit_date.

    Cost: one ``build_realized_total_return_as_of`` call per distinct exit
    date in the horizon set (typically on the order of the number of
    trading dates in the holdout, not the number of (ticker, date, horizon)
    rows) — each call is a single-pass corporate-action adjustment over the
    full price panel, cheap given how sparse corporate actions are.

    Args:
        prices: Long-format DataFrame with columns ``ticker``, ``date``,
            ``close`` (raw, unadjusted).
        corporate_actions: must include ``known_at``/``ex_date`` columns
            (migration 011) — see
            :func:`data.normalization.corporate_actions.build_realized_total_return_as_of`.
        horizons: forward-return horizons in trading sessions.
        timing_policy: score_date -> entry_date execution-lag contract.

    Returns:
        DataFrame with the same columns as :func:`compute_forward_returns`
        (:data:`signals.research.timing.RETURN_SERIES_COLUMNS`), suitable
        for :func:`compute_ic_series`'s ``precomputed_forward_returns``
        argument.
    """
    from data.normalization.corporate_actions import build_realized_total_return_as_of
    from data.universe.calendar import session_close_cutoff

    _validate_prices(prices)

    # Structural pass: entry_date/exit_date/score_date depend only on each
    # ticker's own price calendar, not on corporate-action adjustment — RAW
    # prices are used here purely to enumerate the (ticker, score_date,
    # entry_date, exit_date, horizon_days) rows needed; their forward_return
    # values are discarded and recomputed below from the correctly-adjusted
    # per-exit-date series.
    structure = build_return_series(prices, horizons, timing_policy=timing_policy)
    if structure.empty:
        return structure

    rows: list[dict] = []
    for exit_date, group in structure.groupby("exit_date"):
        exit_cutoff = session_close_cutoff(exit_date)
        entry_date_for_call = group["entry_date"].min()
        adjusted, _meta = build_realized_total_return_as_of(
            prices, corporate_actions, entry_date=entry_date_for_call, exit_cutoff=exit_cutoff
        )
        adj_lookup = adjusted.set_index(["ticker", "date"])["adj_close"]

        for row in group.itertuples(index=False):
            key_entry = (row.ticker, row.entry_date)
            key_exit = (row.ticker, row.exit_date)
            if key_entry not in adj_lookup.index or key_exit not in adj_lookup.index:
                continue
            entry_close = float(adj_lookup.loc[key_entry])
            exit_close = float(adj_lookup.loc[key_exit])
            if entry_close == 0:
                logger.warning(
                    "zero_entry_close_skipped_realized",
                    ticker=row.ticker,
                    entry_date=str(row.entry_date),
                    exit_date=str(row.exit_date),
                )
                continue
            rows.append({
                "ticker": row.ticker,
                "score_date": row.score_date,
                "entry_date": row.entry_date,
                "exit_date": row.exit_date,
                "horizon_days": row.horizon_days,
                "forward_return": exit_close / entry_close - 1.0,
                "timing_policy_id": row.timing_policy_id,
            })

    if not rows:
        return pd.DataFrame(columns=RETURN_SERIES_COLUMNS)
    return pd.DataFrame(rows)[RETURN_SERIES_COLUMNS]


# ─── IC series ────────────────────────────────────────────────────────────────

def compute_ic_series(
    scores: pd.DataFrame,
    prices: Optional[pd.DataFrame],
    score_col: str,
    horizons: Optional[list[int]] = None,
    universe: Optional[object] = None,
    timing_policy: TimingPolicy = DEFAULT_TIMING_POLICY,
    precomputed_forward_returns: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Compute per-score-date cross-sectional IC at multiple forward return horizons.

    Cross-sectional IC on score date *t* is the correlation between tickers'
    scores at *t* and their *h*-session forward returns starting from
    ``entry_date`` (t + ``timing_policy.execution_lag_sessions`` sessions,
    per BUG-009 §2.1 — never *t* itself).

    Args:
        scores: Long-format DataFrame with columns ``ticker``, ``date``,
            and ``score_col``. ``date`` is the score observation date.
        prices: Long-format DataFrame with columns ``ticker``, ``date``,
            ``close``. Use a total-return-adjusted close for research use
            (design plan §2.2); this function performs no adjustment.
        score_col: Column in *scores* to evaluate.
        horizons: Forward return horizons in trading sessions.
            Defaults to ``[1, 5, 10, 21, 63]``.
        universe: a ``data.universe.runtime.PITUniverseLookup`` for
            point-in-time membership enforcement (BUG-008). When provided:
            rows for tickers without knowable membership on the score date,
            the entry date, OR the exit date are excluded (a ticker must be
            a knowable member across its whole score-to-exit window, not
            merely on the score date); a date outside the validated
            coverage window raises ``CoverageGapError``; and a
            post-membership cross-section below ``_MIN_TICKERS_PER_DATE``
            raises ``InsufficientCrossSectionError`` instead of being
            silently dropped. Current-universe snapshots and plain ticker
            lists are rejected with ``CurrentUniverseRejectedError``.
            ``None`` keeps the legacy provisional behavior (see module
            docstring).
        timing_policy: the score_date -> entry_date execution-lag contract
            (BUG-009 §2.1). Defaults to the baseline t+1 convention. The
            identifier is carried through to the output and to
            ``signal_ic_stats`` persistence (§2.4).
        precomputed_forward_returns: when supplied, bypasses this
            function's internal same-series ``compute_forward_returns``
            call and uses this frame directly (must have the same columns
            as :data:`signals.research.timing.RETURN_SERIES_COLUMNS`).
            Callers that need per-exit-date-correct corporate-action
            adjustment (design plan §2.3 — a single global adjustment
            cutoff is only safe for the SCORE series' ratio-cancellation
            argument, not for realized returns; adversarial-review round 4
            finding) build this via
            :func:`compute_realized_forward_returns_as_of` and pass it
            here instead of a shared ``prices`` series. ``prices`` is not
            read at all in this mode and may be ``None``.

    Returns:
        DataFrame with columns:
            ``score_date``, ``horizon_days``, ``ic``, ``rank_ic``,
            ``n_obs``, ``timing_policy_id``

        One row per (score_date, horizon) pair where at least
        ``_MIN_TICKERS_PER_DATE`` tickers had valid scores and forward
        returns.  Without a universe, rows where fewer tickers are
        available are dropped.
    """
    if horizons is None:
        horizons = _DEFAULT_HORIZONS

    _validate_scores(scores, score_col)
    universe_lookup = _validate_universe_arg(universe)

    if precomputed_forward_returns is not None:
        missing_cols = set(RETURN_SERIES_COLUMNS) - set(precomputed_forward_returns.columns)
        if missing_cols:
            raise ValueError(
                f"precomputed_forward_returns is missing columns: {missing_cols}"
            )
        fwd = precomputed_forward_returns
    else:
        _validate_prices(prices)
        fwd = compute_forward_returns(prices, horizons, timing_policy=timing_policy)

    scores_renamed = scores[["ticker", "date", score_col]].rename(columns={"date": "score_date"})
    merged = scores_renamed.merge(fwd, on=["ticker", "score_date"], how="inner")

    if universe_lookup is not None:
        pre_pairs = {
            (dt, int(h))
            for dt, h in merged[["score_date", "horizon_days"]].drop_duplicates().itertuples(index=False)
        }
        merged = _filter_by_membership(merged, universe_lookup)
        post_pairs = {
            (dt, int(h))
            for dt, h in merged[["score_date", "horizon_days"]].drop_duplicates().itertuples(index=False)
        }
        vanished = pre_pairs - post_pairs
        if vanished:
            from data.universe.runtime import InsufficientCrossSectionError

            sample = sorted(vanished)[:5]
            raise InsufficientCrossSectionError(
                f"{len(vanished)} (score_date, horizon) cross-sections lost every ticker "
                f"to membership filtering (e.g. {sample}). Failing closed instead of "
                "emitting IC from a silently shrunken universe (BUG-008)."
            )

    rows: list[dict] = []
    for (dt, h), group in merged.groupby(["score_date", "horizon_days"]):
        valid = group.dropna(subset=[score_col, "forward_return"])
        n = len(valid)
        if n < _MIN_TICKERS_PER_DATE:
            if universe_lookup is not None:
                from data.universe.runtime import InsufficientCrossSectionError

                raise InsufficientCrossSectionError(
                    f"Only {n} member tickers have valid scores and forward returns "
                    f"on score_date {dt} (horizon {h}); minimum is {_MIN_TICKERS_PER_DATE}. "
                    "Failing closed instead of emitting IC from a silently "
                    "shrunken universe (BUG-008)."
                )
            continue

        ic_val = float(
            valid[score_col].corr(valid["forward_return"], method="pearson")
        )
        rank_ic_val = float(
            valid[score_col].corr(valid["forward_return"], method="spearman")
        )

        rows.append({
            "score_date": dt,
            "horizon_days": int(h),
            "ic": ic_val,
            "rank_ic": rank_ic_val,
            "n_obs": n,
            "timing_policy_id": timing_policy.policy_id,
        })

    if not rows:
        return pd.DataFrame(
            columns=["score_date", "horizon_days", "ic", "rank_ic", "n_obs", "timing_policy_id"]
        )

    result = (
        pd.DataFrame(rows)
        .sort_values(["horizon_days", "score_date"])
        .reset_index(drop=True)
    )

    logger.info(
        "ic_series_computed",
        score_col=score_col,
        horizons=horizons,
        timing_policy_id=timing_policy.policy_id,
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
        ic_series: Output of :func:`compute_ic_series`. If it carries a
            ``timing_policy_id`` column (it always does from
            ``compute_ic_series``), the value is propagated into the
            summary's ``timing_policy_id`` column (design plan §2.4: "Add
            the timing-policy identifier to IC summaries").
        factor_name: Factor identifier (e.g. ``'momentum_composite'``).
        strategy_id: Strategy config version identifier.
        eval_date: Last date of the evaluation window.  Defaults to the
            maximum date present in *ic_series*.

    Returns:
        DataFrame with columns:
            ``factor_name``, ``strategy_id``, ``eval_date``,
            ``horizon_days``, ``ic``, ``rank_ic``, ``ic_tstat``,
            ``ic_ir``, ``ic_pvalue``, ``n_observations``, ``timing_policy_id``
        One row per horizon.  Horizons with fewer than
        ``_MIN_IC_DATES_FOR_TSTAT`` observations are excluded.

    Raises:
        ValueError: if *ic_series* carries more than one distinct
            ``timing_policy_id`` — mixing timing policies within one summary
            would make the aggregate statistics incomparable across rows.
    """
    _COLS = [
        "factor_name", "strategy_id", "eval_date", "horizon_days",
        "ic", "rank_ic", "ic_tstat", "ic_ir", "ic_pvalue", "n_observations",
        "timing_policy_id",
    ]
    if ic_series.empty:
        return pd.DataFrame(columns=_COLS)

    if eval_date is None:
        eval_date = ic_series["score_date"].max()

    timing_policy_id: Optional[str] = None
    if "timing_policy_id" in ic_series.columns:
        distinct_policies = ic_series["timing_policy_id"].dropna().unique()
        if len(distinct_policies) > 1:
            raise ValueError(
                f"ic_series carries multiple timing_policy_id values {sorted(distinct_policies)}; "
                "summarize_ic cannot aggregate across mixed timing policies."
            )
        if len(distinct_policies) == 1:
            timing_policy_id = str(distinct_policies[0])

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

        # Overlapping h-day forward returns induce serial correlation in daily
        # IC observations. Estimate the intercept-only mean with Newey-West
        # covariance using h - 1 lags, then apply the one-sided H1: mean_IC > 0.
        hac_lags = max(0, int(h) - 1)
        model = OLS(ic_vals.values, np.ones((n_dates, 1))).fit(
            cov_type="HAC",
            cov_kwds={"maxlags": hac_lags},
        )
        tstat = float(model.tvalues[0])
        pvalue = float(stats.norm.sf(tstat))
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
            "timing_policy_id": timing_policy_id,
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
            ``corrected_pvalue`` — BH/BHY-adjusted p-value (q-value).
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
            DataFrame with columns ``score_date``, ``horizon_days``, ``ic``,
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
    required = {"score_date", "horizon_days", "ic", "rank_ic"}
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
            .sort_values("score_date")
            .reset_index(drop=True)
        )
        ics = sub["ic"].values
        rank_ics = sub["rank_ic"].values
        dates = sub["score_date"].values

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


# ─── Point-in-time universe enforcement (BUG-008) ────────────────────────────

def _validate_universe_arg(universe: Optional[object]):
    """Accept a PITUniverseLookup, reject anything else non-None (BUG-008).

    Type-level enforcement per design plan §1.4: a current-universe loader
    (CurrentUniverseSnapshot) or a plain ticker list cannot be passed to
    historical IC code. Returns the lookup, or None for the legacy
    provisional path.
    """
    if universe is None:
        logger.warning(
            "ic_without_pit_universe",
            note=(
                "compute_ic_series called without a PITUniverseLookup — results "
                "use whatever tickers appear in scores/prices and remain "
                "PROVISIONAL (BUG-008); not valid for selection or promotion"
            ),
        )
        return None

    from data.universe.runtime import CurrentUniverseRejectedError, PITUniverseLookup

    if isinstance(universe, PITUniverseLookup):
        return universe
    raise CurrentUniverseRejectedError(
        f"compute_ic_series requires a PITUniverseLookup for membership "
        f"enforcement; got {type(universe).__name__}. Current-universe snapshots "
        "and plain ticker lists are rejected because their historical membership "
        "provenance cannot be verified (BUG-008)."
    )


def _filter_by_membership(merged: pd.DataFrame, universe_lookup) -> pd.DataFrame:
    """Keep only rows with knowable index membership on score_date, entry_date,
    AND exit_date (BUG-009 §2.5: "membership remains checked on score, entry,
    and exit").  A ticker delisted between entry and exit must not silently
    contribute a realized return computed on data it could not have earned as
    an index member throughout the holding window.

    Raises CoverageGapError (from the lookup) when any of these dates falls
    outside the validated coverage window — historical IC fails closed
    rather than silently scoring uncertified dates.
    """
    # Normalize to plain datetime.date before querying the PIT lookup
    # (Codex PR #34 P2): callers commonly hold pandas Timestamp/datetime64
    # dates from read_sql/CSV/parquet loads, which the coverage-window
    # comparison rejects. The same normalized key is used for row filtering.
    def _as_plain_date(value) -> date:
        if isinstance(value, date) and not isinstance(value, pd.Timestamp):
            return value
        return pd.Timestamp(value).date()

    date_cols = [c for c in ("score_date", "entry_date", "exit_date") if c in merged.columns]

    eligible_by_date: dict = {}
    all_dates = set()
    for col in date_cols:
        all_dates.update(merged[col].unique())
    for dt in all_dates:
        key = _as_plain_date(dt)
        if key not in eligible_by_date:
            result = universe_lookup.load_universe_as_of(key)
            eligible_by_date[key] = set(result.eligible_tickers)

    def _row_eligible(row) -> bool:
        for col in date_cols:
            key = _as_plain_date(getattr(row, col))
            if row.ticker not in eligible_by_date[key]:
                return False
        return True

    columns = ["ticker"] + date_cols
    mask = [_row_eligible(row) for row in merged[columns].itertuples(index=False)]
    filtered = merged[pd.Series(mask, index=merged.index)]
    n_excluded = len(merged) - len(filtered)
    if n_excluded:
        logger.info(
            "ic_rows_excluded_by_membership",
            n_excluded=n_excluded,
            n_kept=len(filtered),
            universe_id=universe_lookup.universe_id,
        )
    return filtered


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
