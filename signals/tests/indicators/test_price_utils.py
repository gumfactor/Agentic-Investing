"""Unit tests for signals/indicators/_price_utils.py — BUG-010 missing-data helpers."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signals.indicators._price_utils import (
    daily_return,
    require_full_window,
    rolling_valid_count,
)


def _wide(rows: dict[str, list[float | None]], start: str = "2020-01-01") -> pd.DataFrame:
    n = len(next(iter(rows.values())))
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame(rows, index=idx)


def test_daily_return_gap_yields_nan_not_zero():
    """A gap between two valid prices produces NaN at the first post-gap return,
    never a fabricated zero (BUG-010 acceptance test)."""
    wide = _wide({"AAA": [100.0, 101.0, None, 103.0, 104.0]})
    ret = daily_return(wide)
    # Row 0 is always NaN (no prior price). Row 2 (the gap itself) is NaN.
    # Row 3 (first return computed across the gap) must be NaN too, not 0.
    assert pd.isna(ret["AAA"].iloc[0])
    assert pd.isna(ret["AAA"].iloc[2])
    assert pd.isna(ret["AAA"].iloc[3])
    assert ret["AAA"].iloc[3] != 0.0 or pd.isna(ret["AAA"].iloc[3])
    # The valid return before the gap is unaffected.
    assert ret["AAA"].iloc[1] == pytest.approx(0.01)
    # The return after the gap resumes correctly once two adjacent valid prices exist.
    assert ret["AAA"].iloc[4] == pytest.approx(104.0 / 103.0 - 1.0)


def test_daily_return_never_forward_fills():
    """Confirms fill_method=None semantics: a long gap does not get bridged."""
    wide = _wide({"AAA": [100.0, None, None, None, 200.0, 202.0]})
    ret = daily_return(wide)
    assert ret["AAA"].iloc[1:5].isna().all()
    # Only once two *consecutive* valid prices exist again does a return
    # appear — pct_change always diffs against the immediately preceding
    # row, which is still NaN for the first valid price after the gap, so
    # no return is fabricated across the gap.
    assert ret["AAA"].iloc[5] == pytest.approx(202.0 / 200.0 - 1.0)


def test_daily_return_rejects_non_positive_price():
    wide = _wide({"AAA": [100.0, 0.0, 101.0]})
    with pytest.raises(ValueError, match="non-positive"):
        daily_return(wide)


def test_daily_return_rejects_negative_price():
    wide = _wide({"AAA": [100.0, -5.0, 101.0]})
    with pytest.raises(ValueError, match="non-positive"):
        daily_return(wide)


def test_daily_return_rejects_infinite_price():
    wide = _wide({"AAA": [100.0, np.inf, 101.0]})
    with pytest.raises(ValueError, match="non-finite"):
        daily_return(wide)


def test_daily_return_allows_missing_price():
    """NaN (missing) is not the same defect as non-positive/infinite — allowed."""
    wide = _wide({"AAA": [100.0, None, 101.0]})
    ret = daily_return(wide)
    assert pd.isna(ret["AAA"].iloc[1])


def test_rolling_valid_count_counts_only_non_missing():
    returns = pd.Series([0.01, np.nan, 0.02, np.nan, 0.03], index=pd.bdate_range("2020-01-01", periods=5))
    counts = rolling_valid_count(returns.to_frame("AAA"), window=3)["AAA"]
    # window ending at index 2 (values 0.01, nan, 0.02) -> 2 valid
    assert counts.iloc[2] == 2
    # window ending at index 4 (values 0.02, nan, 0.03) -> 2 valid
    assert counts.iloc[4] == 2


def test_require_full_window_suppresses_gap_spanning_window():
    """A rolling window whose lookback spans a gap in the underlying returns is
    suppressed (NaN) rather than silently computed from fewer observations —
    the failure mode a cumulative sum or boolean mask would otherwise hide."""
    returns = pd.DataFrame(
        {"AAA": [0.01, np.nan, 0.02, 0.01, 0.01]},
        index=pd.bdate_range("2020-01-01", periods=5),
    )
    # A derived cumulative value that (incorrectly, if left ungated) stays
    # numeric across the gap because cumsum() treats NaN as a zero contribution.
    derived = returns.cumsum()
    gated = require_full_window(derived, returns, window=3)
    # Windows [0:3) and [1:4) touch the NaN return at index 1 -> must be NaN.
    assert pd.isna(gated["AAA"].iloc[2])
    assert pd.isna(gated["AAA"].iloc[3])
    # Window [2:5) = indices 2,3,4, all valid -> passes through.
    assert not pd.isna(gated["AAA"].iloc[4])
    assert gated["AAA"].iloc[4] == derived["AAA"].iloc[4]


def test_require_full_window_all_valid_passes_through():
    returns = pd.DataFrame(
        {"AAA": [0.01, 0.02, 0.01, 0.01, 0.03]},
        index=pd.bdate_range("2020-01-01", periods=5),
    )
    derived = returns.cumsum()
    gated = require_full_window(derived, returns, window=3)
    assert gated["AAA"].iloc[2:].equals(derived["AAA"].iloc[2:])
