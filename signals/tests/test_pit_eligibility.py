"""PIT eligibility cross-section tests for all composite factors (BUG-008).

Codex PR #34 rounds 4-5 established the class rule: membership must define
the cross-section BEFORE any cross-sectional statistic (z-score, rank,
mean/std). Momentum's mask is covered in
data/tests/universe/test_backfill_universe_filter.py; this file covers the
remaining composites — lowvol, value, quality — with the same
contamination-impossibility proof: member scores must be byte-identical
whether an extreme-valued non-member is priced or absent entirely.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from signals.composites.low_vol_score import compute_lowvol_scores
from signals.composites.quality_score import compute_quality_scores
from signals.composites.value_score import compute_value_scores

MEMBERS = [f"M{i:02d}" for i in range(12)]


def _biz_dates(start: date, n: int) -> list[date]:
    dates, d = [], start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return dates


def _member_prices(dates: list[date], seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for t in MEMBERS:
        level = 100.0 + rng.uniform(-20, 20)
        for d in dates:
            level *= 1.0 + rng.normal(0.0002, 0.01)
            rows.append({"ticker": t, "date": d, "close": round(level, 6)})
    return pd.DataFrame(rows)


def _whale_prices(dates: list[date]) -> pd.DataFrame:
    # Wildly volatile non-member: alternating ±20% days.
    rows = []
    level = 50.0
    for i, d in enumerate(dates):
        level *= 1.2 if i % 2 == 0 else 0.8
        rows.append({"ticker": "WHALE", "date": d, "close": round(level, 6)})
    return pd.DataFrame(rows)


def _eligibility(dates: list[date]) -> pd.DataFrame:
    return pd.DataFrame([{"ticker": t, "date": d} for d in dates for t in MEMBERS])


def _sorted_members(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df[df["ticker"].isin(MEMBERS)]
        .sort_values(["ticker", "date"])
        .reset_index(drop=True)
    )


class TestLowvolPITCrossSection:
    def test_non_member_cannot_shift_member_scores(self) -> None:
        dates = _biz_dates(date(2022, 1, 3), 300)
        base = _member_prices(dates)
        elig = _eligibility(dates)

        with_whale = compute_lowvol_scores(
            pd.concat([base, _whale_prices(dates)], ignore_index=True), eligibility=elig
        )
        without_whale = compute_lowvol_scores(base, eligibility=elig)
        pd.testing.assert_frame_equal(_sorted_members(with_whale), _sorted_members(without_whale))

    def test_non_member_never_scored(self) -> None:
        dates = _biz_dates(date(2022, 1, 3), 300)
        prices = pd.concat([_member_prices(dates), _whale_prices(dates)], ignore_index=True)
        scores = compute_lowvol_scores(prices, eligibility=_eligibility(dates))
        assert not (scores["ticker"] == "WHALE").any()

    def test_beta_cross_section_masked_too(self) -> None:
        dates = _biz_dates(date(2022, 1, 3), 300)
        base = _member_prices(dates)
        mkt = pd.DataFrame(
            {"ticker": "SPY", "date": dates, "close": np.linspace(400, 440, len(dates))}
        )
        elig = _eligibility(dates)
        with_whale = compute_lowvol_scores(
            pd.concat([base, _whale_prices(dates)], ignore_index=True),
            market_prices=mkt,
            eligibility=elig,
        )
        without_whale = compute_lowvol_scores(base, market_prices=mkt, eligibility=elig)
        pd.testing.assert_frame_equal(_sorted_members(with_whale), _sorted_members(without_whale))


def _fundamentals(tickers: list[str], whale_extreme: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    rows = []
    items = {
        "net_income": lambda: rng.uniform(0.5e9, 5e9),
        "total_equity": lambda: rng.uniform(5e9, 50e9),
        "total_assets": lambda: rng.uniform(10e9, 100e9),
        "gross_profit": lambda: rng.uniform(1e9, 10e9),
        "operating_cash_flow": lambda: rng.uniform(0.5e9, 5e9),
        "free_cash_flow": lambda: rng.uniform(0.3e9, 4e9),
        "shares_outstanding": lambda: rng.uniform(1e8, 5e9),
    }
    for t in tickers:
        for item, gen in items.items():
            value = gen()
            if whale_extreme and t == "WHALE":
                value = value * 1000  # absurd outlier ratios
            rows.append(
                {
                    "ticker": t,
                    "period_end_date": date(2021, 12, 31),
                    "release_date": date(2022, 2, 1),
                    "period_type": "annual",
                    "item_name": item,
                    "value": Decimal(str(round(value, 2))),
                }
            )
    return pd.DataFrame(rows)


class TestValuePITCrossSection:
    def test_non_member_cannot_shift_member_scores(self) -> None:
        score_date = date(2022, 3, 1)
        dates = [score_date]
        base_prices = _member_prices(dates)
        whale_prices = pd.DataFrame([{"ticker": "WHALE", "date": score_date, "close": 1.0}])
        elig = _eligibility(dates)

        with_whale = compute_value_scores(
            _fundamentals(MEMBERS + ["WHALE"], whale_extreme=True),
            pd.concat([base_prices, whale_prices], ignore_index=True),
            score_dates=[score_date],
            eligibility=elig,
        )
        without_whale = compute_value_scores(
            _fundamentals(MEMBERS),
            base_prices,
            score_dates=[score_date],
            eligibility=elig,
        )
        pd.testing.assert_frame_equal(_sorted_members(with_whale), _sorted_members(without_whale))
        assert not (with_whale["ticker"] == "WHALE").any()

    def test_min_tickers_gate_counts_only_members(self) -> None:
        # 8 members + 7 non-members with fundamentals: with min_tickers=10
        # the PIT cross-section (8) must fail the gate — non-members can't
        # prop up a too-small member universe.
        score_date = date(2022, 3, 1)
        members = MEMBERS[:8]
        extras = [f"X{i}" for i in range(7)]
        prices = pd.DataFrame(
            [{"ticker": t, "date": score_date, "close": 100.0} for t in members + extras]
        )
        elig = pd.DataFrame([{"ticker": t, "date": score_date} for t in members])
        scores = compute_value_scores(
            _fundamentals(members + extras),
            prices,
            score_dates=[score_date],
            min_tickers=10,
            eligibility=elig,
        )
        assert scores.empty


class TestQualityPITCrossSection:
    def test_non_member_cannot_shift_member_scores(self) -> None:
        score_date = date(2022, 3, 1)
        dates = [score_date]
        base_prices = _member_prices(dates)
        whale_prices = pd.DataFrame([{"ticker": "WHALE", "date": score_date, "close": 1.0}])
        elig = _eligibility(dates)

        with_whale = compute_quality_scores(
            _fundamentals(MEMBERS + ["WHALE"], whale_extreme=True),
            pd.concat([base_prices, whale_prices], ignore_index=True),
            score_dates=[score_date],
            eligibility=elig,
        )
        without_whale = compute_quality_scores(
            _fundamentals(MEMBERS),
            base_prices,
            score_dates=[score_date],
            eligibility=elig,
        )
        pd.testing.assert_frame_equal(_sorted_members(with_whale), _sorted_members(without_whale))
        assert not (with_whale["ticker"] == "WHALE").any()
