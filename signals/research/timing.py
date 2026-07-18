"""Signal-to-execution timing contract (BUG-009, design plan section 2).

Baseline convention (design plan section 2.1):

| Event                        | Rule                                             |
|-------------------------------|--------------------------------------------------|
| Score observation              | Session t close.                                  |
| Decision availability          | After the close of t.                             |
| Earliest daily-bar execution   | Session t+1; score_date < entry_date is mandatory.|
| Research entry reference       | Total-return-adjusted close for t+1.              |
| h-session evaluation return    | adj_close[t+1+h] / adj_close[t+1] - 1.            |

This module is the single place that builds (score_date, entry_date,
exit_date) rows from a price series. ``signals.research.ic`` consumes it
instead of the historical same-close ``compute_forward_returns`` logic.

Per-ticker calendar, not global row-shift
------------------------------------------
Each ticker's own valid trading sessions (its own rows in *prices*) define
its horizon. A holiday or missing bar for a DIFFERENT ticker never shifts
another ticker's row positions: horizons are computed per-ticker group, not
on a single shared wide matrix shift (the historical ``compute_forward_returns``
bug this replaces used ``wide.shift(-h)``, which relies on every ticker
sharing identical row positions in the pivoted matrix).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)


class SameDateScoreError(ValueError):
    """Raised when a caller would receive a same-date (or non-forward) entry.

    Design plan section 2.5: "A same-date score passed to any
    return-computation path is rejected."
    """


@dataclass(frozen=True)
class TimingPolicy:
    """Explicit, versioned signal-to-execution timing contract.

    ``policy_id`` is the identifier persisted alongside IC/score records
    (design plan section 2.4 / section 4) — any change to the timing
    convention below must mint a new ``policy_id`` rather than silently
    reinterpreting rows written under an older one.
    """

    policy_id: str
    execution_lag_sessions: int = 1  # score_date -> entry_date lag, in *sessions*

    def __post_init__(self) -> None:
        if self.execution_lag_sessions < 1:
            raise SameDateScoreError(
                "TimingPolicy.execution_lag_sessions must be >= 1: "
                "score_date < entry_date is mandatory (BUG-009 section 2.1)."
            )


# The baseline convention from design plan section 2.1: score at t's close,
# entry at t+1's close (total-return-adjusted), matching the backtester's
# existing score_date < sim_date execution convention.
DEFAULT_TIMING_POLICY = TimingPolicy(policy_id="t_plus_1_close_v1", execution_lag_sessions=1)

RETURN_SERIES_COLUMNS = [
    "ticker",
    "score_date",
    "entry_date",
    "exit_date",
    "horizon_days",
    "forward_return",
    "timing_policy_id",
]


def build_return_series(
    prices: pd.DataFrame,
    horizons: list[int],
    timing_policy: TimingPolicy = DEFAULT_TIMING_POLICY,
) -> pd.DataFrame:
    """Build (score_date, entry_date, exit_date, horizon_days, forward_return) rows.

    Args:
        prices: Long-format DataFrame with columns ``ticker``, ``date``,
            ``close``. ``close`` should be a total-return-adjusted series
            for research use (see ``data.normalization.corporate_actions
            .build_score_price_history_as_of`` /
            ``build_realized_total_return_as_of``); this function performs
            no adjustment itself.
        horizons: forward-return horizons, each >= 1 *session* (not
            calendar days) measured from ``entry_date``.
        timing_policy: the score_date -> entry_date lag contract. Defaults
            to the baseline t+1 convention.

    Returns:
        Long-format DataFrame with columns in :data:`RETURN_SERIES_COLUMNS`.
        ``score_date`` is the close that observed the signal; ``entry_date``
        is ``timing_policy.execution_lag_sessions`` sessions later on THAT
        TICKER's own calendar; ``exit_date`` is ``horizon_days`` further
        sessions after ``entry_date``. Rows whose horizon would extend past
        the ticker's available price history are dropped (no fabricated
        NaN forward return).

    Raises:
        ValueError: on missing/empty input or a non-positive horizon.
        SameDateScoreError: defensively, if the computed entry_date is not
            strictly after score_date (should be structurally impossible
            given ``execution_lag_sessions >= 1`` and monotonic dates, but
            enforced explicitly per design plan section 2.5).
    """
    required = {"ticker", "date", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices DataFrame missing columns: {missing}")
    if prices.empty:
        raise ValueError("prices DataFrame is empty")
    if any(h < 1 for h in horizons):
        raise ValueError(f"all horizons must be >= 1 session, got {horizons}")

    lag = timing_policy.execution_lag_sessions
    frames: list[pd.DataFrame] = []

    for ticker, group in prices.sort_values("date").groupby("ticker"):
        group = group.drop_duplicates(subset="date").reset_index(drop=True)
        dates = group["date"].tolist()
        closes = group["close"].astype(float).tolist()
        n = len(dates)

        for h in horizons:
            rows = []
            for i in range(n):
                entry_idx = i + lag
                exit_idx = entry_idx + h
                if exit_idx >= n:
                    continue  # horizon extends past this ticker's own history

                score_date = dates[i]
                entry_date = dates[entry_idx]
                exit_date = dates[exit_idx]

                if not (score_date < entry_date < exit_date):
                    # Structurally unreachable given lag >= 1, h >= 1, and a
                    # sorted/deduplicated per-ticker date index — but this is
                    # exactly the BUG-009 invariant, so it is enforced
                    # explicitly rather than trusted to construction.
                    raise SameDateScoreError(
                        f"{ticker}: computed non-forward dates "
                        f"score_date={score_date} entry_date={entry_date} "
                        f"exit_date={exit_date}; score_date < entry_date < "
                        "exit_date is mandatory (BUG-009 section 2.1)."
                    )

                entry_close = closes[entry_idx]
                exit_close = closes[exit_idx]
                if entry_close == 0:
                    continue
                forward_return = exit_close / entry_close - 1.0

                rows.append({
                    "ticker": ticker,
                    "score_date": score_date,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "horizon_days": h,
                    "forward_return": forward_return,
                    "timing_policy_id": timing_policy.policy_id,
                })
            if rows:
                frames.append(pd.DataFrame(rows))

    if not frames:
        return pd.DataFrame(columns=RETURN_SERIES_COLUMNS)

    return pd.concat(frames, ignore_index=True)[RETURN_SERIES_COLUMNS]


def reject_same_date(score_date: date, entry_date: date) -> None:
    """Explicit guard: raise :class:`SameDateScoreError` unless score_date < entry_date.

    Callers building a return path outside :func:`build_return_series`
    (e.g. a single hand-built entry/exit pair) should call this before use
    (design plan section 2.5: "A same-date score passed to any
    return-computation path is rejected").
    """
    if not score_date < entry_date:
        raise SameDateScoreError(
            f"score_date {score_date} must be strictly before entry_date "
            f"{entry_date} (BUG-009 section 2.1)."
        )
