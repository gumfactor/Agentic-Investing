"""Reliability and validity diagnostic for factor scores.

Checks whether computed indicators measure what they claim to measure,
consistently, before those factors are used in strategy construction.

Reliability checks (per factor)
--------------------------------
- NaN/coverage rate: fraction of scored rows where z_score is null.
- z-score calibration: cross-sectional mean should be ≈ 0 and std ≈ 1 per
  date, since every indicator is supposed to be cross-sectionally
  standardised.  Persistent bias or wrong spread is a computation bug.
- Outlier rate: fraction of scores with |z| > 4.  A well-behaved
  cross-sectional z-score should almost never be that extreme.
- Rank persistence: median Spearman rank autocorrelation between consecutive
  score dates.  Too low → the signal is mostly noise; suspiciously close to
  1.0 → scores may be frozen (stale data, caching bug).

Validity checks (cross-factor)
--------------------------------
- Convergent validity: factors in the same conceptual category should
  correlate positively.  A momentum composite and a breakout signal that
  show |r| < 0.30 are probably not measuring the same thing.
- Discriminant validity: factors from *different* categories should not be
  highly correlated.  High cross-category |r| means one factor is redundant
  with another.
- High-correlation pairs: any two factors with |r| > 0.75 are flagged as
  potential duplicates regardless of category.

Usage::

    from backtesting.validation.indicator_diagnostic import (
        IndicatorDiagnostic,
        format_report,
    )

    diag = IndicatorDiagnostic()
    report = diag.run(factor_scores_df, strategy_id="v1_base_momentum")
    print(format_report(report))
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# ─── Default thresholds ───────────────────────────────────────────────────────

_MAX_NAN_RATE: float = 0.20
_MAX_MEAN_BIAS: float = 0.10
_MIN_STD: float = 0.70
_MAX_STD: float = 1.40
_MAX_OUTLIER_RATE: float = 0.01
_MIN_RANK_AUTOCORR: float = 0.50
_MAX_RANK_AUTOCORR: float = 0.995
_HIGH_CORR_THRESHOLD: float = 0.75
_MIN_WITHIN_CATEGORY_CORR: float = 0.30
_MAX_CROSS_CATEGORY_CORR: float = 0.65
_MIN_TICKERS_FOR_AUTOCORR: int = 5

# ─── Category inference ───────────────────────────────────────────────────────

# Keywords are matched against the lower-cased factor name.  Longer keywords
# take priority — "deep_value" beats "value".
_CATEGORY_KEYWORDS: list[tuple[str, str]] = sorted(
    [
        ("deep_value", "value"),
        ("risk_adjusted_value", "value"),
        ("quality_value", "value"),
        ("income_yield", "value"),
        ("low_vol_momentum", "momentum"),
        ("small_cap_momentum", "momentum"),
        ("relative_strength", "momentum"),
        ("trend_strength", "momentum"),
        ("oscillator_agreement", "momentum"),
        ("short_term_reversal", "momentum"),
        ("volume_momentum", "momentum"),
        ("breakout", "momentum"),
        ("momentum", "momentum"),
        ("defensive_quality", "quality"),
        ("compounding_quality", "quality"),
        ("small_cap_quality", "quality"),
        ("financial_fortress", "quality"),
        ("earnings_conviction", "quality"),
        ("piotroski", "quality"),
        ("quality_dip", "quality"),
        ("quality", "quality"),
        ("sustainable_growth", "growth"),
        ("small_cap_growth", "growth"),
        ("growth", "growth"),
        ("garp", "growth"),
        ("low_vol", "volatility"),
        ("volume", "volume"),
        ("vol", "volatility"),
        ("value", "value"),
    ],
    key=lambda kv: -len(kv[0]),
)


def infer_category(factor_name: str) -> str:
    """Infer a factor's conceptual category from its name.

    Longer keyword matches take priority over shorter ones.  Returns ``"other"``
    for factor names that do not match any known keyword.

    Args:
        factor_name: Factor identifier string (e.g. ``"momentum_score"``).

    Returns:
        Category string such as ``"momentum"``, ``"value"``, ``"quality"``,
        ``"growth"``, ``"volatility"``, ``"volume"``, or ``"other"``.
    """
    lower = factor_name.lower()
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in lower:
            return category
    return "other"


# ─── Result dataclasses ───────────────────────────────────────────────────────

@dataclass
class FactorReliability:
    """Reliability metrics for a single factor.

    Attributes:
        factor_name: Factor identifier (matches ``factor_name`` column in DB).
        category: Inferred conceptual category.
        n_observations: Number of non-null scored (ticker, date) pairs.
        nan_rate: Fraction of rows where z_score is null.
        mean_bias: Mean of per-date cross-sectional means.  Should be ≈ 0.
        std_mean: Mean of per-date cross-sectional standard deviations.
            Should be ≈ 1 for a properly standardised factor.
        std_stability: Std of the per-date std series.  Lower means the
            cross-sectional spread is consistent over time.
        outlier_rate: Fraction of scores with |z| > 4.
        median_rank_autocorr: Median Spearman rank autocorrelation between
            consecutive score dates.
        flags: Human-readable descriptions of any reliability issues found.
        reliable: True if no flags were raised.
    """

    factor_name: str
    category: str
    n_observations: int
    nan_rate: float
    mean_bias: float
    std_mean: float
    std_stability: float
    outlier_rate: float
    median_rank_autocorr: float
    flags: list[str]
    reliable: bool


@dataclass
class ValidityResult:
    """Cross-factor validity metrics.

    Attributes:
        correlation_matrix: Pairwise Spearman correlation (factor × factor).
        within_category_mean: Mean absolute correlation among same-category pairs.
        cross_category_mean: Mean absolute correlation among cross-category pairs.
        high_correlation_pairs: Pairs with |r| above the high-correlation
            threshold (potential redundancy, regardless of category).
        low_within_category_pairs: Same-category pairs with |r| below the
            within-category threshold (same-category factors disagree).
        flags: Human-readable validity warnings.
    """

    correlation_matrix: pd.DataFrame
    within_category_mean: float
    cross_category_mean: float
    high_correlation_pairs: list[tuple[str, str, float]]
    low_within_category_pairs: list[tuple[str, str, float]]
    flags: list[str]


@dataclass
class DiagnosticReport:
    """Full diagnostic report for a set of factor scores.

    Attributes:
        strategy_id: Strategy this factor set belongs to.
        n_factors: Number of distinct factors evaluated.
        n_dates: Number of distinct score dates in the sample.
        n_tickers: Number of distinct tickers in the sample.
        date_range: (first_date, last_date) of the sample.
        reliability: Per-factor reliability results.
        validity: Cross-factor validity results.
        n_reliable: Number of factors that cleared all reliability checks.
        summary: One-line overall verdict.
        min_within_category_corr: Threshold used to flag low within-category pairs.
        max_cross_category_corr: Threshold used to flag high cross-category mean.
    """

    strategy_id: str
    n_factors: int
    n_dates: int
    n_tickers: int
    date_range: tuple[date, date]
    reliability: list[FactorReliability]
    validity: ValidityResult
    n_reliable: int
    summary: str
    min_within_category_corr: float
    max_cross_category_corr: float


# ─── Diagnostic engine ────────────────────────────────────────────────────────

class IndicatorDiagnostic:
    """Reliability and validity diagnostic for factor scores.

    All threshold arguments are optional — defaults match the module-level
    constants and can be tightened or relaxed per use-case.

    Args:
        max_nan_rate: Flag factors whose NaN rate exceeds this.
        max_mean_bias: Flag factors whose mean cross-sectional bias exceeds this.
        min_std: Flag factors whose mean cross-sectional std is below this.
        max_std: Flag factors whose mean cross-sectional std is above this.
        max_outlier_rate: Flag factors with more than this fraction of |z| > 4.
        min_rank_autocorr: Flag factors with median rank autocorr below this.
        max_rank_autocorr: Flag factors with median rank autocorr above this.
        high_corr_threshold: Flag factor pairs with |r| above this.
        min_within_category_corr: Flag same-category pairs with |r| below this.
        max_cross_category_corr: Flag cross-category pairs with |r| above this.
    """

    def __init__(
        self,
        max_nan_rate: float = _MAX_NAN_RATE,
        max_mean_bias: float = _MAX_MEAN_BIAS,
        min_std: float = _MIN_STD,
        max_std: float = _MAX_STD,
        max_outlier_rate: float = _MAX_OUTLIER_RATE,
        min_rank_autocorr: float = _MIN_RANK_AUTOCORR,
        max_rank_autocorr: float = _MAX_RANK_AUTOCORR,
        high_corr_threshold: float = _HIGH_CORR_THRESHOLD,
        min_within_category_corr: float = _MIN_WITHIN_CATEGORY_CORR,
        max_cross_category_corr: float = _MAX_CROSS_CATEGORY_CORR,
    ) -> None:
        self._max_nan_rate = max_nan_rate
        self._max_mean_bias = max_mean_bias
        self._min_std = min_std
        self._max_std = max_std
        self._max_outlier_rate = max_outlier_rate
        self._min_rank_autocorr = min_rank_autocorr
        self._max_rank_autocorr = max_rank_autocorr
        self._high_corr_threshold = high_corr_threshold
        self._min_within_category_corr = min_within_category_corr
        self._max_cross_category_corr = max_cross_category_corr

    def run(
        self,
        factor_scores: pd.DataFrame,
        strategy_id: str = "unknown",
    ) -> DiagnosticReport:
        """Run all reliability and validity checks.

        Args:
            factor_scores: Long-format DataFrame with columns ``ticker``,
                ``score_date``, ``factor_name``, ``z_score``.  Additional
                columns are ignored.
            strategy_id: Label for the report.

        Returns:
            :class:`DiagnosticReport` with per-factor reliability and
            cross-factor validity results.

        Raises:
            ValueError: If required columns are missing or the DataFrame is empty.
        """
        _validate_input(factor_scores)

        factors = sorted(factor_scores["factor_name"].unique())
        dates = sorted(factor_scores["score_date"].unique())
        tickers = sorted(factor_scores["ticker"].unique())

        logger.info(
            "indicator_diagnostic_start",
            strategy_id=strategy_id,
            n_factors=len(factors),
            n_dates=len(dates),
            n_tickers=len(tickers),
        )

        reliability = [
            self._check_factor_reliability(factor_scores, f)
            for f in factors
        ]

        wide = _pivot_wide(factor_scores, factors)
        validity = self._check_validity(wide, factors)

        n_reliable = sum(1 for r in reliability if r.reliable)
        n_flags = sum(len(r.flags) for r in reliability) + len(validity.flags)
        summary = (
            f"{n_reliable}/{len(factors)} factors reliable; "
            f"{n_flags} total flag(s) raised"
        )

        return DiagnosticReport(
            strategy_id=strategy_id,
            n_factors=len(factors),
            n_dates=len(dates),
            n_tickers=len(tickers),
            date_range=(dates[0], dates[-1]),
            reliability=reliability,
            validity=validity,
            n_reliable=n_reliable,
            summary=summary,
            min_within_category_corr=self._min_within_category_corr,
            max_cross_category_corr=self._max_cross_category_corr,
        )

    # ─── Reliability ──────────────────────────────────────────────────────────

    def _check_factor_reliability(
        self,
        factor_scores: pd.DataFrame,
        factor_name: str,
    ) -> FactorReliability:
        sub = factor_scores[factor_scores["factor_name"] == factor_name]
        category = infer_category(factor_name)
        flags: list[str] = []

        total = len(sub)
        n_valid = int(sub["z_score"].notna().sum())
        nan_rate = 1.0 - n_valid / total if total > 0 else 1.0

        if nan_rate > self._max_nan_rate:
            flags.append(
                f"high NaN rate {nan_rate:.1%} "
                f"(threshold {self._max_nan_rate:.1%})"
            )

        valid = sub.dropna(subset=["z_score"])

        # ── Cross-sectional calibration ────────────────────────────────────
        per_date = (
            valid.groupby("score_date")["z_score"]
            .agg(["mean", "std"])
            .dropna()
        )
        if per_date.empty:
            mean_bias = float("nan")
            std_mean = float("nan")
            std_stability = float("nan")
        else:
            mean_bias = float(per_date["mean"].mean())
            std_mean = float(per_date["std"].mean())
            std_stability = (
                float(per_date["std"].std(ddof=1))
                if len(per_date) > 1
                else 0.0
            )

        if not math.isnan(mean_bias) and abs(mean_bias) > self._max_mean_bias:
            flags.append(
                f"mean z-score bias {mean_bias:+.3f} "
                f"(threshold ±{self._max_mean_bias:.2f})"
            )
        if not math.isnan(std_mean):
            if std_mean < self._min_std:
                flags.append(
                    f"cross-sectional std {std_mean:.3f} below minimum "
                    f"{self._min_std} — scores are under-dispersed"
                )
            elif std_mean > self._max_std:
                flags.append(
                    f"cross-sectional std {std_mean:.3f} above maximum "
                    f"{self._max_std} — scores are over-dispersed"
                )

        # ── Outlier rate ───────────────────────────────────────────────────
        if n_valid > 0:
            outlier_rate = float((valid["z_score"].abs() > 4.0).mean())
        else:
            outlier_rate = float("nan")

        if not math.isnan(outlier_rate) and outlier_rate > self._max_outlier_rate:
            flags.append(
                f"outlier rate {outlier_rate:.2%} with |z| > 4 "
                f"(threshold {self._max_outlier_rate:.1%})"
            )

        # ── Rank persistence ───────────────────────────────────────────────
        median_rank_autocorr = _compute_rank_autocorr(valid)

        if not math.isnan(median_rank_autocorr):
            if median_rank_autocorr < self._min_rank_autocorr:
                flags.append(
                    f"rank autocorr {median_rank_autocorr:.3f} below minimum "
                    f"{self._min_rank_autocorr} — signal may be too noisy"
                )
            elif median_rank_autocorr > self._max_rank_autocorr:
                flags.append(
                    f"rank autocorr {median_rank_autocorr:.3f} above maximum "
                    f"{self._max_rank_autocorr} — scores may be frozen"
                )

        return FactorReliability(
            factor_name=factor_name,
            category=category,
            n_observations=n_valid,
            nan_rate=nan_rate,
            mean_bias=mean_bias,
            std_mean=std_mean,
            std_stability=std_stability,
            outlier_rate=outlier_rate,
            median_rank_autocorr=median_rank_autocorr,
            flags=flags,
            reliable=len(flags) == 0,
        )

    # ─── Validity ─────────────────────────────────────────────────────────────

    def _check_validity(
        self,
        wide: pd.DataFrame,
        factors: list[str],
    ) -> ValidityResult:
        flags: list[str] = []

        if wide.shape[1] < 2:
            return ValidityResult(
                correlation_matrix=pd.DataFrame(),
                within_category_mean=float("nan"),
                cross_category_mean=float("nan"),
                high_correlation_pairs=[],
                low_within_category_pairs=[],
                flags=["fewer than 2 factors — cannot compute correlation structure"],
            )

        corr = wide.corr(method="spearman", numeric_only=True)
        categories = {f: infer_category(f) for f in factors}

        within_corrs: list[float] = []
        cross_corrs: list[float] = []
        high_pairs: list[tuple[str, str, float]] = []
        low_within_pairs: list[tuple[str, str, float]] = []

        n = len(factors)
        for i in range(n):
            for j in range(i + 1, n):
                fi, fj = factors[i], factors[j]
                if fi not in corr.index or fj not in corr.columns:
                    continue
                r = float(corr.loc[fi, fj])
                if math.isnan(r):
                    continue
                abs_r = abs(r)
                same_cat = categories[fi] == categories[fj]
                fi_known = categories[fi] != "other"
                fj_known = categories[fj] != "other"

                if same_cat and fi_known:
                    within_corrs.append(abs_r)
                    if abs_r < self._min_within_category_corr:
                        low_within_pairs.append((fi, fj, r))
                elif not same_cat and fi_known and fj_known:
                    cross_corrs.append(abs_r)

                if abs_r > self._high_corr_threshold:
                    high_pairs.append((fi, fj, r))

        within_mean = float(np.mean(within_corrs)) if within_corrs else float("nan")
        cross_mean = float(np.mean(cross_corrs)) if cross_corrs else float("nan")

        if not math.isnan(within_mean) and within_mean < self._min_within_category_corr:
            flags.append(
                f"mean within-category |r| = {within_mean:.3f} "
                f"(target > {self._min_within_category_corr}) — "
                "same-category factors may not agree"
            )
        if not math.isnan(cross_mean) and cross_mean > self._max_cross_category_corr:
            flags.append(
                f"mean cross-category |r| = {cross_mean:.3f} "
                f"(target < {self._max_cross_category_corr}) — "
                "factors from different categories may be redundant"
            )
        if high_pairs:
            preview = ", ".join(
                f"{a}/{b} ({r:.2f})" for a, b, r in high_pairs[:5]
            )
            suffix = f" (+{len(high_pairs) - 5} more)" if len(high_pairs) > 5 else ""
            flags.append(
                f"{len(high_pairs)} high-correlation pair(s) with |r| > "
                f"{self._high_corr_threshold}: {preview}{suffix}"
            )
        if low_within_pairs:
            preview = ", ".join(
                f"{a}/{b} ({r:.2f})" for a, b, r in low_within_pairs[:5]
            )
            suffix = (
                f" (+{len(low_within_pairs) - 5} more)"
                if len(low_within_pairs) > 5
                else ""
            )
            flags.append(
                f"{len(low_within_pairs)} within-category pair(s) with |r| < "
                f"{self._min_within_category_corr}: {preview}{suffix}"
            )

        return ValidityResult(
            correlation_matrix=corr,
            within_category_mean=within_mean,
            cross_category_mean=cross_mean,
            high_correlation_pairs=high_pairs,
            low_within_category_pairs=low_within_pairs,
            flags=flags,
        )


# ─── Report formatter ─────────────────────────────────────────────────────────

def format_report(report: DiagnosticReport) -> str:
    """Format a :class:`DiagnosticReport` as a human-readable string."""
    W = 72
    sep = "─" * W
    lines: list[str] = []

    lines.append(sep)
    lines.append("INDICATOR DIAGNOSTIC REPORT")
    lines.append(sep)
    lines.append(f"Strategy : {report.strategy_id}")
    lines.append(
        f"Period   : {report.date_range[0]} → {report.date_range[1]}"
    )
    lines.append(
        f"Universe : {report.n_tickers} tickers  "
        f"{report.n_dates} dates  "
        f"{report.n_factors} factors"
    )
    lines.append(f"Summary  : {report.summary}")
    lines.append("")

    # ── Reliability table ─────────────────────────────────────────────────
    lines.append("RELIABILITY")
    lines.append(sep)
    lines.append(
        f"{'Factor':<30} {'Cat':<11} "
        f"{'NaN%':>5} {'Bias':>7} {'Std':>5} {'Out%':>5} {'AutoCr':>7}  Status"
    )
    lines.append("─" * W)

    sorted_rel = sorted(
        report.reliability, key=lambda r: (not r.reliable, r.factor_name)
    )
    for r in sorted_rel:
        status = "OK" if r.reliable else f"WARN ({len(r.flags)})"
        bias_s = f"{r.mean_bias:+.3f}" if not math.isnan(r.mean_bias) else "  n/a"
        std_s = f"{r.std_mean:.3f}" if not math.isnan(r.std_mean) else " n/a"
        out_s = f"{r.outlier_rate:.2%}" if not math.isnan(r.outlier_rate) else "  n/a"
        ac_s = (
            f"{r.median_rank_autocorr:.3f}"
            if not math.isnan(r.median_rank_autocorr)
            else "  n/a"
        )
        lines.append(
            f"{r.factor_name:<30} {r.category:<11} "
            f"{r.nan_rate:>4.1%} {bias_s:>7} {std_s:>5} {out_s:>5} {ac_s:>7}  {status}"
        )
        for flag in r.flags:
            lines.append(f"    ! {flag}")

    lines.append("")

    # ── Validity summary ──────────────────────────────────────────────────
    lines.append("VALIDITY (cross-factor correlation structure)")
    lines.append(sep)
    v = report.validity
    if not math.isnan(v.within_category_mean):
        lines.append(
            f"Within-category mean |r| : {v.within_category_mean:.3f}"
            f"  (target > {report.min_within_category_corr})"
        )
    if not math.isnan(v.cross_category_mean):
        lines.append(
            f"Cross-category  mean |r| : {v.cross_category_mean:.3f}"
            f"  (target < {report.max_cross_category_corr})"
        )
    if v.flags:
        lines.append("Flags:")
        for flag in v.flags:
            lines.append(f"  ! {flag}")
    else:
        lines.append("  No validity flags raised.")

    lines.append(sep)
    return "\n".join(lines)


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _validate_input(df: pd.DataFrame) -> None:
    required = {"ticker", "score_date", "factor_name", "z_score"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"factor_scores is missing required columns: {missing}"
        )
    if df.empty:
        raise ValueError("factor_scores DataFrame is empty")
    n_dupes = df.duplicated(subset=["ticker", "score_date", "factor_name"]).sum()
    if n_dupes > 0:
        logger.warning(
            "indicator_diagnostic_duplicate_rows",
            n_duplicates=int(n_dupes),
            msg=(
                "Duplicate (ticker, score_date, factor_name) rows detected. "
                "pivot_table will silently average them. "
                "Deduplicate before running diagnostics for accurate results."
            ),
        )


def _pivot_wide(factor_scores: pd.DataFrame, factors: list[str]) -> pd.DataFrame:
    """Pivot long factor_scores to wide format (ticker×date rows, factor columns)."""
    return (
        factor_scores[["ticker", "score_date", "factor_name", "z_score"]]
        .pivot_table(
            index=["ticker", "score_date"],
            columns="factor_name",
            values="z_score",
        )
        .reindex(columns=factors)
    )


def _compute_rank_autocorr(valid: pd.DataFrame) -> float:
    """Compute median cross-sectional rank autocorrelation between consecutive dates.

    For each pair of consecutive score dates, finds the tickers present on both
    dates, ranks them by z_score, and computes the Spearman correlation of those
    ranks.  The median across all consecutive-date pairs is returned.

    Returns NaN if fewer than two dates have sufficient data.
    """
    if "score_date" not in valid.columns or valid.empty:
        return float("nan")

    wide = (
        valid[["ticker", "score_date", "z_score"]]
        .pivot_table(index="score_date", columns="ticker", values="z_score")
        .sort_index()
    )

    dates = list(wide.index)
    if len(dates) < 2:
        return float("nan")

    autocorrs: list[float] = []
    for i in range(1, len(dates)):
        curr = wide.iloc[i].dropna()
        prev = wide.iloc[i - 1].dropna()
        common = curr.index.intersection(prev.index)
        if len(common) < _MIN_TICKERS_FOR_AUTOCORR:
            continue
        rho = float(curr[common].rank().corr(prev[common].rank()))
        if not math.isnan(rho):
            autocorrs.append(rho)

    return float(np.median(autocorrs)) if autocorrs else float("nan")
