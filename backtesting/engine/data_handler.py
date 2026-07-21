"""Point-in-time safe data access for the backtesting simulation.

DataHandler wraps pre-loaded DataFrames and enforces the temporal horizon:
only data whose observation date is <= simulation_date is visible to the engine.
Alpha scores use a stricter filter — score_date < sim_date (strictly less-than)
to enforce a 1-day execution lag: signals computed from day-t closing prices
are only tradeable from day t+1 onwards.

No DB I/O occurs here — the caller loads data and passes DataFrames in.
This separation makes the engine fully testable without a database.

Raw execution series vs. analytic series (BUG-070, design plan §2.2/§2.4)
---------------------------------------------------------------------------
``prices`` is the RAW (unadjusted) tradable close. ``get_close`` returns this
series and is the ONLY series the engine may use for order fills, cash, and
share accounting -- a corporate action's effect on a held position must be
applied explicitly (split -> share-count change, dividend -> cash), via
``get_corporate_actions_on``, never by silently trading against an adjusted
price (that would misstate notional/shares actually traded).

``analytic_prices`` is an OPTIONAL cutoff-aware or full-history
total-return-adjusted series (built by the caller with
``data.normalization.corporate_actions.build_score_price_history_as_of`` or
``build_realized_total_return_as_of``) used only for total-return valuation
and reporting/comparison -- never for fills. ``get_analytic_close`` exposes
it; when not supplied it falls back to the raw close (no adjustment
available), which callers must not mistake for an adjusted series.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)


class DataHandler:
    """PIT-safe data access for backtesting.

    Args:
        prices: Long-format DataFrame with columns ticker, date, open, high,
            low, close, volume. date column must be castable to datetime.date.
            This is the RAW (unadjusted) tradable close -- the only series
            used for fills/cash/share accounting (BUG-070).
        alpha_scores: Long-format DataFrame with columns ticker, score_date,
            alpha_score (and optionally rank, universe_size, strategy_id).
            score_date is the date the score was computed; no future scores
            are visible on simulation dates before score_date.
        benchmark: Long-format DataFrame with columns date, close for a single
            benchmark ticker (e.g. SPY). Used to compute benchmark returns.
        corporate_actions: Optional long-format DataFrame with columns
            ticker, ex_date, action_type ('split'|'dividend'|'spinoff'),
            value. Used ONLY for explicit portfolio-side accounting (split ->
            share-count change, dividend -> cash) via
            ``get_corporate_actions_on`` -- never to adjust the raw price
            series. Defaults to an empty frame (no actions) when omitted.
        analytic_prices: Optional long-format DataFrame with columns ticker,
            date, close (a total-return-adjusted close, e.g. from
            ``build_realized_total_return_as_of``). Exposed via
            ``get_analytic_close`` for total-return valuation/reporting only.
    """

    def __init__(
        self,
        prices: pd.DataFrame,
        alpha_scores: pd.DataFrame,
        benchmark: pd.DataFrame,
        corporate_actions: Optional[pd.DataFrame] = None,
        analytic_prices: Optional[pd.DataFrame] = None,
    ) -> None:
        self._prices = _normalise_date_col(prices.copy(), "date")
        self._alpha_scores = _normalise_date_col(alpha_scores.copy(), "score_date")
        self._benchmark = _normalise_date_col(benchmark.copy(), "date")

        _require_cols(self._prices, {"ticker", "date", "close"}, "prices")
        _require_cols(self._alpha_scores, {"ticker", "score_date", "alpha_score"}, "alpha_scores")
        _require_cols(self._benchmark, {"date", "close"}, "benchmark")

        self._prices["close"] = self._prices["close"].astype(float)
        self._benchmark["close"] = self._benchmark["close"].astype(float)

        if corporate_actions is None:
            corporate_actions = pd.DataFrame(
                columns=["ticker", "ex_date", "action_type", "value"]
            )
        _require_cols(
            corporate_actions, {"ticker", "ex_date", "action_type", "value"}, "corporate_actions"
        )
        self._corporate_actions = _normalise_date_col(corporate_actions.copy(), "ex_date")

        if analytic_prices is not None:
            analytic_prices = _normalise_date_col(analytic_prices.copy(), "date")
            _require_cols(analytic_prices, {"ticker", "date", "close"}, "analytic_prices")
            analytic_prices["close"] = analytic_prices["close"].astype(float)
        self._analytic_prices = analytic_prices

        self._sorted_dates: list[date] = sorted(self._prices["date"].unique())

        self._validate_corporate_action_calendar_alignment()

    def _validate_corporate_action_calendar_alignment(self) -> None:
        """Fail closed if any within-window corporate-action ex_date has no
        aligned trading date in the loaded price calendar (BUG-070, P1).

        The event loop only visits sim_dates present in the price calendar
        and applies corporate actions via ``get_corporate_actions_on(sim_
        date)``. An action whose ex_date is a real trading day that is
        MISSING from the loaded price snapshot would therefore be silently
        dropped -- permanently corrupting the share count (e.g. a 2:1 split
        never applied => 2x undercount) for every subsequent day. A silently
        dropped split is exactly the corruption this gate exists to stop, so
        it must raise, not warn.

        Scope: only ex_dates that fall WITHIN the loaded price window
        [min, max] are checked. An action outside the window is correctly
        never applied (there is no in-window position it could affect) and
        is not an error. An empty price or corporate_actions frame is a
        no-op.
        """
        if self._corporate_actions.empty or not self._sorted_dates:
            return
        window_start = self._sorted_dates[0]
        window_end = self._sorted_dates[-1]
        trading_dates = set(self._sorted_dates)

        ex_dates = self._corporate_actions["ex_date"]
        within_window = (ex_dates >= window_start) & (ex_dates <= window_end)
        misaligned = sorted(
            {
                d
                for d in self._corporate_actions.loc[within_window, "ex_date"]
                if d not in trading_dates
            }
        )
        if misaligned:
            raise ValueError(
                "corporate_actions contains ex_date(s) within the loaded "
                f"price window [{window_start}, {window_end}] that have no "
                f"aligned trading date in the price calendar: {misaligned}. "
                "The backtest engine only applies a corporate action on a "
                "sim_date present in the price snapshot, so such an action "
                "would be silently dropped -- permanently corrupting share "
                "counts (e.g. a dropped 2:1 split undercounts shares 2x for "
                "every later day). Failing closed (BUG-070): re-pin the "
                "snapshot so the ex_date's trading session is present, or "
                "correct the corporate_actions ex_date."
            )

    # ------------------------------------------------------------------
    # Core accessors
    # ------------------------------------------------------------------

    def get_close(self, sim_date: date) -> dict[str, float]:
        """Closing prices for all tickers on sim_date."""
        mask = self._prices["date"] == sim_date
        day = self._prices[mask]
        return dict(zip(day["ticker"], day["close"]))

    def get_latest_signals(self, sim_date: date) -> pd.DataFrame:
        """Most-recent alpha_score per ticker visible on sim_date.

        Enforces PIT with a 1-day execution lag: scores computed from day t's
        closing prices are only tradeable from day t+1 onwards.  Using the same
        close to both compute a signal and fill the resulting order is look-ahead
        bias — the signal did not exist before the close printed.

        Only scores with score_date < sim_date (strictly less than) are returned.
        Returns a DataFrame with columns ticker, score_date, alpha_score.
        """
        mask = self._alpha_scores["score_date"] < sim_date
        visible = self._alpha_scores[mask]
        if visible.empty:
            return pd.DataFrame(columns=["ticker", "score_date", "alpha_score"])
        latest = (
            visible.sort_values("score_date")
            .groupby("ticker", sort=False)
            .last()
            .reset_index()[["ticker", "score_date", "alpha_score"]]
        )
        return latest

    def get_corporate_actions_on(self, sim_date: date) -> pd.DataFrame:
        """Corporate-action rows with ``ex_date == sim_date`` (BUG-070).

        Returned columns: ticker, action_type, value. Used exclusively for
        explicit portfolio-side accounting (split -> share-count change,
        dividend -> cash) -- never to adjust the raw execution price series.
        Empty when no actions were supplied to this DataHandler or none fall
        on ``sim_date``.
        """
        mask = self._corporate_actions["ex_date"] == sim_date
        return self._corporate_actions.loc[mask, ["ticker", "action_type", "value"]].reset_index(drop=True)

    def get_analytic_close(self, sim_date: date) -> dict[str, float]:
        """Total-return-adjusted closing prices for all tickers on sim_date.

        For total-return valuation/reporting ONLY -- never for fills, cash,
        or share accounting (BUG-070, design plan §2.2). Falls back to the
        raw close series when no ``analytic_prices`` was supplied to this
        DataHandler (no adjustment available; callers must not treat the
        fallback as an adjusted series).
        """
        if self._analytic_prices is None:
            return self.get_close(sim_date)
        mask = self._analytic_prices["date"] == sim_date
        day = self._analytic_prices[mask]
        return dict(zip(day["ticker"], day["close"]))

    def get_benchmark_return(self, sim_date: date) -> Optional[float]:
        """Daily return of the benchmark on sim_date. None if date not found."""
        bm = self._benchmark.sort_values("date")
        idx = bm.index[bm["date"] == sim_date].tolist()
        if not idx:
            return None
        pos = bm.index.get_loc(idx[0])
        if pos == 0:
            return None
        prev_close = bm.iloc[pos - 1]["close"]
        curr_close = bm.iloc[pos]["close"]
        if prev_close <= 0:
            return None
        return float(curr_close / prev_close - 1.0)

    def get_benchmark_returns_series(self, start: date, end: date) -> pd.Series:
        """Daily benchmark returns for [start, end]. Index = date.

        Uses fill_method=None (BUG-010) so a missing benchmark session
        produces NaN — dropped by the trailing .dropna() — rather than being
        forward-filled into a fabricated zero return that could distort
        downstream Sharpe/beta comparisons.
        """
        bm = (
            self._benchmark[
                (self._benchmark["date"] >= start) & (self._benchmark["date"] <= end)
            ]
            .sort_values("date")
            .set_index("date")["close"]
        )
        return bm.pct_change(fill_method=None).dropna()

    # ------------------------------------------------------------------
    # Date utilities
    # ------------------------------------------------------------------

    def trading_dates(self, start: date, end: date) -> list[date]:
        """All trading dates in [start, end] that appear in the price data."""
        return [d for d in self._sorted_dates if start <= d <= end]

    def rebalance_dates(
        self,
        start: date,
        end: date,
        frequency: str = "monthly",
    ) -> list[date]:
        """Trading dates where rebalancing should occur.

        frequency:
            'daily'   – every trading day
            'weekly'  – every 5th trading day starting from the first
            'monthly' – first trading day of each calendar month
        """
        all_dates = self.trading_dates(start, end)
        if not all_dates:
            return []
        if frequency == "daily":
            return all_dates
        if frequency == "weekly":
            return [d for i, d in enumerate(all_dates) if i % 5 == 0]
        if frequency == "monthly":
            result: list[date] = []
            last_month: Optional[int] = None
            for d in all_dates:
                if d.month != last_month:
                    result.append(d)
                    last_month = d.month
            return result
        raise ValueError(f"Unknown rebalance frequency: {frequency!r}")


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _normalise_date_col(df: pd.DataFrame, col: str) -> pd.DataFrame:
    df[col] = pd.to_datetime(df[col]).dt.date
    return df


def _require_cols(df: pd.DataFrame, required: set[str], name: str) -> None:
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{name} DataFrame missing required columns: {missing}")
