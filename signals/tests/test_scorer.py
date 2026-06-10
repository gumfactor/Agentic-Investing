"""Tests for signals/scoring/scorer.py."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from signals.scoring.scorer import combine_factor_scores


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _biz_dates(start: date, n: int) -> list[date]:
    dates, d = [], start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return dates


def _make_factor_df(
    tickers: list[str],
    dates: list[date],
    score_col: str,
    seed: int = 0,
) -> pd.DataFrame:
    """Build a synthetic factor DataFrame in long format."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in dates:
        scores = rng.standard_normal(len(tickers))
        for ticker, s in zip(tickers, scores):
            rows.append({"ticker": ticker, "date": d, score_col: float(s)})
    return pd.DataFrame(rows)


TICKERS = [f"T{i:02d}" for i in range(10)]
DATES = _biz_dates(date(2022, 1, 3), 20)


# ─── combine_factor_scores ────────────────────────────────────────────────────

class TestCombineFactorScores:
    def test_returns_two_dataframes(self):
        momentum = _make_factor_df(TICKERS, DATES[:5], "momentum_score")
        factor_df, alpha_df = combine_factor_scores(
            factor_scores={"momentum": momentum},
            score_col_map={"momentum": "momentum_score"},
            strategy_id="v1",
        )
        assert isinstance(factor_df, pd.DataFrame)
        assert isinstance(alpha_df, pd.DataFrame)

    def test_factor_scores_df_columns(self):
        momentum = _make_factor_df(TICKERS, DATES[:3], "momentum_score")
        factor_df, _ = combine_factor_scores(
            {"momentum": momentum},
            {"momentum": "momentum_score"},
            strategy_id="v1",
        )
        assert set(factor_df.columns) >= {
            "ticker", "score_date", "factor_name", "strategy_id", "z_score"
        }

    def test_alpha_scores_df_columns(self):
        momentum = _make_factor_df(TICKERS, DATES[:3], "momentum_score")
        _, alpha_df = combine_factor_scores(
            {"momentum": momentum},
            {"momentum": "momentum_score"},
            strategy_id="v1",
        )
        assert set(alpha_df.columns) >= {
            "ticker", "score_date", "strategy_id", "alpha_score", "rank", "universe_size"
        }

    def test_strategy_id_propagated(self):
        momentum = _make_factor_df(TICKERS, DATES[:3], "momentum_score")
        factor_df, alpha_df = combine_factor_scores(
            {"momentum": momentum},
            {"momentum": "momentum_score"},
            strategy_id="test_strategy",
        )
        assert (factor_df["strategy_id"] == "test_strategy").all()
        assert (alpha_df["strategy_id"] == "test_strategy").all()

    def test_empty_input_returns_empty_dataframes(self):
        factor_df, alpha_df = combine_factor_scores(
            factor_scores={},
            score_col_map={},
            strategy_id="v1",
        )
        assert factor_df.empty
        assert alpha_df.empty

    def test_empty_factor_df_skipped(self):
        """An empty factor DataFrame should be skipped without error."""
        momentum = _make_factor_df(TICKERS, DATES[:3], "momentum_score")
        factor_df, alpha_df = combine_factor_scores(
            factor_scores={"momentum": momentum, "value": pd.DataFrame()},
            score_col_map={"momentum": "momentum_score", "value": "value_score"},
            strategy_id="v1",
        )
        assert not alpha_df.empty
        # Only momentum in factor_df (value was empty)
        assert "momentum" in factor_df["factor_name"].values

    def test_two_factors_equal_weight_blend(self):
        """With two factors, alpha = mean of their scores."""
        tickers = ["A", "B", "C"]
        d = date(2022, 1, 3)

        momentum = pd.DataFrame([
            {"ticker": "A", "date": d, "momentum_score": 1.0},
            {"ticker": "B", "date": d, "momentum_score": 0.0},
            {"ticker": "C", "date": d, "momentum_score": -1.0},
        ])
        lowvol = pd.DataFrame([
            {"ticker": "A", "date": d, "lowvol_score": 0.0},
            {"ticker": "B", "date": d, "lowvol_score": 1.0},
            {"ticker": "C", "date": d, "lowvol_score": -1.0},
        ])

        _, alpha_df = combine_factor_scores(
            {"momentum": momentum, "lowvol": lowvol},
            {"momentum": "momentum_score", "lowvol": "lowvol_score"},
            strategy_id="v1",
            score_date=d,
        )

        alpha_a = alpha_df[alpha_df["ticker"] == "A"]["alpha_score"].iloc[0]
        alpha_b = alpha_df[alpha_df["ticker"] == "B"]["alpha_score"].iloc[0]
        alpha_c = alpha_df[alpha_df["ticker"] == "C"]["alpha_score"].iloc[0]

        assert abs(alpha_a - 0.5) < 1e-6, f"A expected 0.5, got {alpha_a}"
        assert abs(alpha_b - 0.5) < 1e-6, f"B expected 0.5, got {alpha_b}"
        assert abs(alpha_c - (-1.0)) < 1e-6, f"C expected -1.0, got {alpha_c}"

    def test_weighted_blend(self):
        """Weighted blend should use normalised weights."""
        tickers = ["A", "B"]
        d = date(2022, 1, 3)
        momentum = pd.DataFrame([
            {"ticker": "A", "date": d, "momentum_score": 2.0},
            {"ticker": "B", "date": d, "momentum_score": 0.0},
        ])
        lowvol = pd.DataFrame([
            {"ticker": "A", "date": d, "lowvol_score": 0.0},
            {"ticker": "B", "date": d, "lowvol_score": 2.0},
        ])
        # Weight momentum 3×, lowvol 1×  → normalised: 0.75, 0.25
        _, alpha_df = combine_factor_scores(
            {"momentum": momentum, "lowvol": lowvol},
            {"momentum": "momentum_score", "lowvol": "lowvol_score"},
            strategy_id="v1",
            score_date=d,
            weights={"momentum": 3.0, "lowvol": 1.0},
        )
        alpha_a = alpha_df[alpha_df["ticker"] == "A"]["alpha_score"].iloc[0]
        expected_a = 2.0 * 0.75 + 0.0 * 0.25
        assert abs(alpha_a - expected_a) < 1e-6

    def test_rank_descending_best_first(self):
        """Rank 1 should be assigned to the ticker with the highest alpha_score."""
        tickers = ["A", "B", "C"]
        d = date(2022, 1, 3)
        scores = pd.DataFrame([
            {"ticker": "A", "date": d, "momentum_score": 2.0},  # best
            {"ticker": "B", "date": d, "momentum_score": 1.0},
            {"ticker": "C", "date": d, "momentum_score": -1.0},
        ])
        _, alpha_df = combine_factor_scores(
            {"momentum": scores},
            {"momentum": "momentum_score"},
            strategy_id="v1",
            score_date=d,
        )
        rank_a = alpha_df[alpha_df["ticker"] == "A"]["rank"].iloc[0]
        assert rank_a == 1, f"A should be rank 1, got {rank_a}"

    def test_universe_size_correct(self):
        tickers = TICKERS[:5]
        d = date(2022, 1, 3)
        scores = pd.DataFrame([
            {"ticker": t, "date": d, "momentum_score": float(i)}
            for i, t in enumerate(tickers)
        ])
        _, alpha_df = combine_factor_scores(
            {"momentum": scores},
            {"momentum": "momentum_score"},
            strategy_id="v1",
            score_date=d,
        )
        assert (alpha_df["universe_size"] == 5).all()

    def test_score_date_filter(self):
        """Only scores for the requested score_date should appear in alpha_df."""
        d1, d2 = DATES[0], DATES[1]
        scores = _make_factor_df(TICKERS, [d1, d2], "momentum_score")
        _, alpha_df = combine_factor_scores(
            {"momentum": scores},
            {"momentum": "momentum_score"},
            strategy_id="v1",
            score_date=d1,
        )
        assert set(alpha_df["score_date"].unique()) == {d1}

    def test_nan_alpha_rows_dropped(self):
        """Rows where alpha_score is NaN should not appear in alpha_scores_df."""
        d = date(2022, 1, 3)
        scores = pd.DataFrame([
            {"ticker": "A", "date": d, "momentum_score": float("nan")},
            {"ticker": "B", "date": d, "momentum_score": 1.0},
        ])
        _, alpha_df = combine_factor_scores(
            {"momentum": scores},
            {"momentum": "momentum_score"},
            strategy_id="v1",
            score_date=d,
        )
        assert "A" not in alpha_df["ticker"].values

    def test_missing_score_col_logs_warning_and_skips(self):
        """A factor with a mismatched score_col_map entry should be skipped."""
        scores = _make_factor_df(TICKERS, DATES[:2], "momentum_score")
        # Wrong column name in map
        factor_df, alpha_df = combine_factor_scores(
            {"momentum": scores},
            {"momentum": "wrong_col"},
            strategy_id="v1",
        )
        assert factor_df.empty
        assert alpha_df.empty

    def test_graceful_degradation_one_factor_empty(self):
        """If one of two factors is empty, the composite uses the available one."""
        momentum = _make_factor_df(TICKERS, DATES[:3], "momentum_score")
        _, alpha_df = combine_factor_scores(
            {"momentum": momentum, "value": pd.DataFrame()},
            {"momentum": "momentum_score", "value": "value_score"},
            strategy_id="v1",
        )
        assert not alpha_df.empty
