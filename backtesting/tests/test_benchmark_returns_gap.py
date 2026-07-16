"""BUG-010 regression: DataHandler benchmark returns must not fabricate a
zero return from a missing benchmark close.

Before the fix, ``get_benchmark_returns_series`` used ``pct_change()`` with
pandas' legacy ``fill_method='pad'`` default: a NaN benchmark close was
forward-filled before differencing, producing a fabricated 0.0 return on the
gap session and a wrong (padded-base) return on the following session.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backtesting.engine.data_handler import DataHandler


def _handler_with_benchmark(benchmark: pd.DataFrame) -> DataHandler:
    prices = pd.DataFrame(
        {
            "ticker": ["AAA"] * len(benchmark),
            "date": benchmark["date"],
            "close": [100.0] * len(benchmark),
        }
    )
    alpha = pd.DataFrame(
        {"ticker": ["AAA"], "score_date": [benchmark["date"].iloc[0]], "alpha_score": [1.0]}
    )
    return DataHandler(prices=prices, alpha_scores=alpha, benchmark=benchmark)


def test_benchmark_gap_yields_no_fabricated_zero_return():
    dates = [date(2024, 1, d) for d in (2, 3, 4, 5, 8)]
    benchmark = pd.DataFrame(
        {"date": dates, "close": [100.0, 101.0, float("nan"), 104.0, 106.0]}
    )
    handler = _handler_with_benchmark(benchmark)

    returns = handler.get_benchmark_returns_series(dates[0], dates[-1])

    # The gap session and the session immediately after it (whose diff spans
    # the gap) must be absent — not fabricated as 0.0 / a padded-base return.
    assert date(2024, 1, 4) not in returns.index
    assert date(2024, 1, 5) not in returns.index
    assert not (returns == 0.0).any()
    # The two well-defined returns survive.
    assert returns[date(2024, 1, 3)] == pytest.approx(0.01)
    assert returns[date(2024, 1, 8)] == pytest.approx(106.0 / 104.0 - 1.0)
    assert len(returns) == 2
