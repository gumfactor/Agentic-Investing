"""Tests for backtesting.validation.indicator_diagnostic."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from backtesting.validation.indicator_diagnostic import (
    DiagnosticReport,
    FactorReliability,
    IndicatorDiagnostic,
    ValidityResult,
    _compute_rank_autocorr,
    _pivot_wide,
    format_report,
    infer_category,
)


# ─── Fixtures / helpers ───────────────────────────────────────────────────────

_TICKERS = [f"T{i:03d}" for i in range(20)]
_START = date(2023, 1, 3)


def _business_dates(start: date, n: int) -> list[date]:
    dates: list[date] = []
    d = start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return dates


def _make_factor_scores(
    factors: dict[str, float],
    tickers: list[str] = _TICKERS,
    n_days: int = 60,
    nan_rate: float = 0.0,
    seed: int = 42,
) -> pd.DataFrame:
    """Build synthetic factor_scores with controllable properties.

    Args:
        factors: mapping factor_name → autocorrelation parameter (0–1).
            Higher values produce more persistent scores.
        tickers: ticker list.
        n_days: number of score dates.
        nan_rate: fraction of rows to null out.
        seed: RNG seed.
    """
    rng = np.random.default_rng(seed)
    dates = _business_dates(_START, n_days)
    rows: list[dict] = []

    for factor_name, autocorr in factors.items():
        # Initialise cross-section as standard normal
        prev = rng.standard_normal(len(tickers))
        for d in dates:
            noise = rng.standard_normal(len(tickers))
            scores = autocorr * prev + math.sqrt(1 - autocorr**2) * noise
            # Cross-sectional standardise so mean≈0, std≈1
            scores = (scores - scores.mean()) / (scores.std() + 1e-9)
            prev = scores
            for ticker, z in zip(tickers, scores):
                rows.append({
                    "ticker": ticker,
                    "score_date": d,
                    "factor_name": factor_name,
                    "z_score": z,
                })

    df = pd.DataFrame(rows)
    if nan_rate > 0.0:
        mask = rng.random(len(df)) < nan_rate
        df.loc[mask, "z_score"] = float("nan")
    return df


# ─── Category inference ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("momentum_score", "momentum"),
    ("breakout_momentum", "momentum"),
    ("relative_strength", "momentum"),
    ("trend_strength", "momentum"),
    ("oscillator_agreement", "momentum"),
    ("short_term_reversal", "momentum"),
    ("volume_momentum", "momentum"),
    ("low_vol_momentum", "momentum"),
    ("value_score", "value"),
    ("deep_value_oversold", "value"),
    ("risk_adjusted_value", "value"),
    ("income_yield", "value"),
    ("quality_score", "quality"),
    ("piotroski_f_score", "quality"),
    ("financial_fortress", "quality"),
    ("defensive_quality", "quality"),
    ("compounding_quality", "quality"),
    ("earnings_conviction", "quality"),
    ("quality_dip", "quality"),
    ("growth_score", "growth"),
    ("sustainable_growth", "growth"),
    ("garp", "growth"),
    ("low_vol_score", "volatility"),
    # Real volatility indicator names — must resolve to "volatility" via "vol" keyword
    ("realized_vol_21d_score", "volatility"),
    ("vol_percentile_252d_score", "volatility"),
    ("idiosyncratic_vol_63d_score", "volatility"),
    ("garman_klass_vol_21d_score", "volatility"),
    ("atr_14_score", "other"),            # "atr" has no keyword match
    ("downside_deviation_63d_score", "other"),  # no keyword match
    # "volume" should not bleed into "volatility"
    ("volume_score", "volume"),
    ("totally_unknown_factor", "other"),
])
def test_infer_category(name: str, expected: str) -> None:
    assert infer_category(name) == expected


def test_infer_category_case_insensitive() -> None:
    assert infer_category("MOMENTUM_SCORE") == "momentum"
    assert infer_category("Quality_Score") == "quality"


# ─── Reliability: clean data passes all checks ────────────────────────────────

def test_clean_factor_passes_all_reliability_checks() -> None:
    df = _make_factor_scores({"momentum_score": 0.85})
    diag = IndicatorDiagnostic()
    report = diag.run(df, strategy_id="test")
    rel = report.reliability[0]

    assert rel.reliable is True
    assert rel.flags == []
    assert rel.nan_rate == pytest.approx(0.0)
    assert abs(rel.mean_bias) < 0.10
    assert 0.70 <= rel.std_mean <= 1.40
    assert rel.outlier_rate < 0.01
    assert 0.50 <= rel.median_rank_autocorr <= 0.995


# ─── Reliability: NaN rate ────────────────────────────────────────────────────

def test_high_nan_rate_raises_flag() -> None:
    df = _make_factor_scores({"momentum_score": 0.80}, nan_rate=0.50)
    diag = IndicatorDiagnostic(max_nan_rate=0.20)
    report = diag.run(df)
    rel = report.reliability[0]

    assert rel.reliable is False
    assert any("NaN rate" in f for f in rel.flags)


def test_nan_rate_below_threshold_passes() -> None:
    df = _make_factor_scores({"momentum_score": 0.80}, nan_rate=0.05)
    diag = IndicatorDiagnostic(max_nan_rate=0.20)
    report = diag.run(df)
    assert report.reliability[0].reliable is True


# ─── Reliability: z-score calibration ────────────────────────────────────────

def test_large_mean_bias_raises_flag() -> None:
    df = _make_factor_scores({"value_score": 0.80})
    df["z_score"] = df["z_score"] + 0.5    # inject a systematic bias
    diag = IndicatorDiagnostic(max_mean_bias=0.10)
    report = diag.run(df)
    rel = report.reliability[0]

    assert rel.reliable is False
    assert any("bias" in f for f in rel.flags)


def test_inflated_std_raises_flag() -> None:
    df = _make_factor_scores({"quality_score": 0.80})
    df["z_score"] = df["z_score"] * 3.0    # inflate the spread
    diag = IndicatorDiagnostic(max_std=1.40)
    report = diag.run(df)
    rel = report.reliability[0]

    assert rel.reliable is False
    assert any("over-dispersed" in f for f in rel.flags)


def test_deflated_std_raises_flag() -> None:
    df = _make_factor_scores({"growth_score": 0.80})
    df["z_score"] = df["z_score"] * 0.1    # compress the spread
    diag = IndicatorDiagnostic(min_std=0.70)
    report = diag.run(df)
    rel = report.reliability[0]

    assert rel.reliable is False
    assert any("under-dispersed" in f for f in rel.flags)


# ─── Reliability: outlier rate ────────────────────────────────────────────────

def test_high_outlier_rate_raises_flag() -> None:
    df = _make_factor_scores({"breakout_momentum": 0.80})
    # Force 5% of scores to an extreme value
    rng = np.random.default_rng(99)
    idx = rng.choice(len(df), size=int(len(df) * 0.05), replace=False)
    df.loc[idx, "z_score"] = 10.0
    diag = IndicatorDiagnostic(max_outlier_rate=0.01)
    report = diag.run(df)
    rel = report.reliability[0]

    assert rel.reliable is False
    assert any("|z| > 4" in f for f in rel.flags)


# ─── Reliability: rank autocorrelation ───────────────────────────────────────

def test_low_rank_autocorr_raises_flag() -> None:
    # autocorr=0.0 → purely random scores each date (no persistence)
    df = _make_factor_scores({"momentum_score": 0.0})
    diag = IndicatorDiagnostic(min_rank_autocorr=0.50)
    report = diag.run(df)
    rel = report.reliability[0]

    assert rel.reliable is False
    assert any("too noisy" in f for f in rel.flags)


def test_frozen_scores_raises_flag() -> None:
    df = _make_factor_scores({"momentum_score": 0.80})
    # Freeze scores: every date has identical values per ticker
    first_date = df["score_date"].min()
    baseline = df[df["score_date"] == first_date][["ticker", "z_score"]].set_index("ticker")
    df["z_score"] = df["ticker"].map(baseline["z_score"])

    diag = IndicatorDiagnostic(max_rank_autocorr=0.995)
    report = diag.run(df)
    rel = report.reliability[0]

    assert rel.reliable is False
    assert any("frozen" in f for f in rel.flags)


# ─── Reliability: multiple factors ───────────────────────────────────────────

def test_multiple_factors_reported_independently() -> None:
    df = _make_factor_scores({
        "momentum_score": 0.85,   # good
        "value_score": 0.80,      # good
    })
    # Corrupt value_score by injecting NaNs
    mask = df["factor_name"] == "value_score"
    df.loc[mask, "z_score"] = float("nan")

    diag = IndicatorDiagnostic()
    report = diag.run(df)
    assert report.n_factors == 2

    results = {r.factor_name: r for r in report.reliability}
    assert results["momentum_score"].reliable is True
    assert results["value_score"].reliable is False


# ─── Validity: convergent ─────────────────────────────────────────────────────

def test_convergent_validity_same_category_correlated() -> None:
    # Build two momentum factors that track the same underlying signal
    df1 = _make_factor_scores({"momentum_score": 0.90}, seed=1)
    # Build a second momentum factor as a noisy copy of the first
    wide = df1.pivot_table(index="score_date", columns="ticker", values="z_score")
    rng = np.random.default_rng(2)
    noise = rng.standard_normal(wide.shape) * 0.3
    wide2 = wide + noise
    wide2 = wide2.apply(lambda col: (col - col.mean()) / (col.std() + 1e-9))
    long2 = wide2.reset_index().melt(
        id_vars="score_date", var_name="ticker", value_name="z_score"
    )
    long2["factor_name"] = "breakout_momentum"

    combined = pd.concat([df1, long2[df1.columns]], ignore_index=True)
    diag = IndicatorDiagnostic(min_within_category_corr=0.30)
    report = diag.run(combined)

    # Highly correlated same-category factors → within_category_mean should be high
    assert report.validity.within_category_mean > 0.30
    # No low-within-category flag expected
    assert not any("within-category" in f and "< 0.30" in f for f in report.validity.flags)


def test_low_within_category_correlation_raises_flag() -> None:
    # Two momentum factors that are completely uncorrelated (bad)
    df1 = _make_factor_scores({"momentum_score": 0.80}, seed=10)
    df2 = _make_factor_scores({"breakout_momentum": 0.80}, seed=99)
    combined = pd.concat([df1, df2], ignore_index=True)

    diag = IndicatorDiagnostic(min_within_category_corr=0.90)
    report = diag.run(combined)

    assert any("within-category" in f for f in report.validity.flags)


# ─── Validity: discriminant ───────────────────────────────────────────────────

def test_high_cross_category_correlation_raises_flag() -> None:
    # momentum and value factor that are nearly identical (bad)
    df1 = _make_factor_scores({"momentum_score": 0.85}, seed=5)
    long_value = df1.copy()
    long_value["factor_name"] = "value_score"
    combined = pd.concat([df1, long_value], ignore_index=True)

    diag = IndicatorDiagnostic(max_cross_category_corr=0.65)
    report = diag.run(combined)

    # Perfect correlation between categories → should flag
    assert any("cross-category" in f for f in report.validity.flags) or \
           any("high-correlation" in f for f in report.validity.flags)


# ─── Validity: high-correlation pairs ────────────────────────────────────────

def test_high_correlation_pair_detected() -> None:
    df1 = _make_factor_scores({"momentum_score": 0.85}, seed=7)
    near_copy = df1.copy()
    near_copy["factor_name"] = "trend_strength"
    near_copy["z_score"] = near_copy["z_score"] + np.random.default_rng(8).normal(
        0, 0.01, len(near_copy)
    )
    combined = pd.concat([df1, near_copy], ignore_index=True)

    diag = IndicatorDiagnostic(high_corr_threshold=0.75)
    report = diag.run(combined)

    assert len(report.validity.high_correlation_pairs) >= 1
    names_in_pairs = {
        name
        for a, b, _ in report.validity.high_correlation_pairs
        for name in (a, b)
    }
    assert "momentum_score" in names_in_pairs or "trend_strength" in names_in_pairs


# ─── Validity: single-factor edge case ───────────────────────────────────────

def test_single_factor_validity_reports_gracefully() -> None:
    df = _make_factor_scores({"momentum_score": 0.85})
    diag = IndicatorDiagnostic()
    report = diag.run(df)

    assert math.isnan(report.validity.within_category_mean)
    assert math.isnan(report.validity.cross_category_mean)
    assert any("fewer than 2 factors" in f for f in report.validity.flags)


# ─── _compute_rank_autocorr ───────────────────────────────────────────────────

def test_rank_autocorr_with_frozen_scores_returns_near_one() -> None:
    dates = _business_dates(_START, 10)
    rows = []
    for d in dates:
        for i, t in enumerate(_TICKERS[:10]):
            rows.append({"ticker": t, "score_date": d, "z_score": float(i)})
    df = pd.DataFrame(rows)
    rho = _compute_rank_autocorr(df)
    assert rho > 0.99


def test_rank_autocorr_returns_nan_for_single_date() -> None:
    rows = [
        {"ticker": t, "score_date": _START, "z_score": float(i)}
        for i, t in enumerate(_TICKERS[:10])
    ]
    df = pd.DataFrame(rows)
    rho = _compute_rank_autocorr(df)
    assert math.isnan(rho)


def test_rank_autocorr_returns_nan_for_empty_df() -> None:
    df = pd.DataFrame(columns=["ticker", "score_date", "z_score"])
    rho = _compute_rank_autocorr(df)
    assert math.isnan(rho)


# ─── _pivot_wide ─────────────────────────────────────────────────────────────

def test_pivot_wide_shape() -> None:
    df = _make_factor_scores({"mom": 0.80, "val": 0.80})
    factors = ["mom", "val"]
    wide = _pivot_wide(df, factors)

    assert set(wide.columns) == {"mom", "val"}
    assert len(wide) == len(_TICKERS) * 60


# ─── Full run / summary ───────────────────────────────────────────────────────

def test_full_run_clean_data_produces_correct_summary() -> None:
    df = _make_factor_scores({
        "momentum_score": 0.85,
        "value_score": 0.80,
        "quality_score": 0.75,
    })
    diag = IndicatorDiagnostic()
    report = diag.run(df, strategy_id="test_strat")

    assert report.strategy_id == "test_strat"
    assert report.n_factors == 3
    assert report.n_dates == 60
    assert report.n_tickers == len(_TICKERS)
    assert report.n_reliable == 3
    assert "3/3 factors reliable" in report.summary


def test_summary_reflects_flagged_factors() -> None:
    df = _make_factor_scores({
        "momentum_score": 0.85,   # clean
        "value_score": 0.0,       # low autocorr → flagged
    })
    diag = IndicatorDiagnostic()
    report = diag.run(df)

    assert report.n_reliable < report.n_factors


# ─── Input validation ─────────────────────────────────────────────────────────

def test_missing_columns_raises_value_error() -> None:
    df = pd.DataFrame({"ticker": ["A"], "score_date": [_START]})
    with pytest.raises(ValueError, match="missing required columns"):
        IndicatorDiagnostic().run(df)


def test_empty_dataframe_raises_value_error() -> None:
    df = pd.DataFrame(
        columns=["ticker", "score_date", "factor_name", "z_score"]
    )
    with pytest.raises(ValueError, match="empty"):
        IndicatorDiagnostic().run(df)


# ─── format_report ────────────────────────────────────────────────────────────

def test_format_report_is_non_empty_string() -> None:
    df = _make_factor_scores({"momentum_score": 0.85})
    report = IndicatorDiagnostic().run(df, strategy_id="fmt_test")
    text = format_report(report)

    assert isinstance(text, str)
    assert len(text) > 100
    assert "INDICATOR DIAGNOSTIC REPORT" in text
    assert "momentum_score" in text
    assert "RELIABILITY" in text
    assert "VALIDITY" in text


def test_format_report_shows_flags() -> None:
    df = _make_factor_scores({"momentum_score": 0.0})  # low autocorr → flagged
    report = IndicatorDiagnostic().run(df)
    text = format_report(report)

    assert "WARN" in text
    assert "!" in text


def test_format_report_displays_actual_thresholds_not_module_constants() -> None:
    # Three factors: two momentum (produces within-category pair) plus one value
    # (produces cross-category pair). Both threshold lines must appear in the report.
    df = _make_factor_scores({
        "momentum_score": 0.85,
        "breakout_momentum": 0.80,
        "value_score": 0.75,
    })
    custom_within = 0.55
    custom_cross = 0.45
    diag = IndicatorDiagnostic(
        min_within_category_corr=custom_within,
        max_cross_category_corr=custom_cross,
    )
    report = diag.run(df, strategy_id="threshold_test")
    text = format_report(report)

    assert str(custom_within) in text
    assert str(custom_cross) in text
    assert report.min_within_category_corr == custom_within
    assert report.max_cross_category_corr == custom_cross


# ─── Validity: "other" category excluded from cross_corrs ────────────────────

def test_other_category_factors_excluded_from_cross_category_mean() -> None:
    # Build one known-category factor and one "other" factor
    df_mom = _make_factor_scores({"momentum_score": 0.85}, seed=1)
    df_unk = _make_factor_scores({"totally_unknown_xyz": 0.80}, seed=77)
    combined = pd.concat([df_mom, df_unk], ignore_index=True)

    diag = IndicatorDiagnostic()
    report = diag.run(combined)

    # "other" factors must not contribute to cross_category_mean
    assert math.isnan(report.validity.cross_category_mean), (
        "cross_category_mean should be NaN when one of the two factors is 'other'"
    )


def test_two_other_category_factors_produce_nan_cross_category_mean() -> None:
    df1 = _make_factor_scores({"unknown_a": 0.80}, seed=10)
    df2 = _make_factor_scores({"unknown_b": 0.80}, seed=20)
    combined = pd.concat([df1, df2], ignore_index=True)

    diag = IndicatorDiagnostic()
    report = diag.run(combined)

    assert math.isnan(report.validity.cross_category_mean)
    assert math.isnan(report.validity.within_category_mean)


# ─── Duplicate row warning ────────────────────────────────────────────────────

def test_duplicate_rows_do_not_raise_but_complete(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    df = _make_factor_scores({"momentum_score": 0.85})
    df_duped = pd.concat([df, df.head(10)], ignore_index=True)

    diag = IndicatorDiagnostic()
    # Should not raise — duplicates are warned but not fatal
    with caplog.at_level(logging.WARNING):
        report = diag.run(df_duped)
    assert report.n_factors == 1
