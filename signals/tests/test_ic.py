"""Tests for signals/research/ic.py."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from signals.research.ic import (
    chronological_split,
    compute_forward_returns,
    compute_ic_series,
    compute_realized_forward_returns_as_of,
    multiple_testing_correction,
    summarize_ic,
)
from signals.research.timing import DEFAULT_TIMING_POLICY, SameDateScoreError, TimingPolicy


# ─── Fixtures / helpers ───────────────────────────────────────────────────────

def _business_dates(start: date, n: int) -> list[date]:
    """Generate n weekday dates starting from start."""
    dates = []
    d = start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return dates


def _make_prices(tickers: list[str], n_days: int, seed: int = 42) -> pd.DataFrame:
    """Generate synthetic log-normal price series."""
    rng = np.random.default_rng(seed)
    dates = _business_dates(date(2021, 1, 4), n_days)
    rows = []
    for i, ticker in enumerate(tickers):
        price = 100.0
        daily_drift = 0.0005 * (i - len(tickers) / 2)  # cross-sectional spread
        for d in dates:
            ret = daily_drift + rng.normal(0, 0.015)
            price *= np.exp(ret)
            rows.append({"ticker": ticker, "date": d, "close": price})
    return pd.DataFrame(rows)


def _make_scores(
    prices: pd.DataFrame,
    score_col: str = "score",
    seed: int = 0,
) -> pd.DataFrame:
    """Assign random cross-sectional scores that have a weak signal."""
    rng = np.random.default_rng(seed)
    rows = []
    for d, group in prices.groupby("date"):
        tickers = group["ticker"].tolist()
        scores = rng.standard_normal(len(tickers))
        for ticker, s in zip(tickers, scores):
            rows.append({"ticker": ticker, "date": d, score_col: s})
    return pd.DataFrame(rows)


def _make_predictive_scores(
    prices: pd.DataFrame,
    horizon: int,
    score_col: str = "score",
    noise: float = 0.5,
    seed: int = 7,
) -> pd.DataFrame:
    """Scores that are correlated with future returns (positive IC expected)."""
    rng = np.random.default_rng(seed)
    wide = (
        prices.pivot_table(index="date", columns="ticker", values="close")
        .sort_index()
    )
    wide.columns.name = None
    fwd_ret = (wide.shift(-horizon) / wide - 1.0).dropna()

    rows = []
    for d, ret_row in fwd_ret.iterrows():
        valid = ret_row.dropna()
        if len(valid) < 5:
            continue
        signal = valid + noise * pd.Series(rng.standard_normal(len(valid)), index=valid.index)
        for ticker, s in signal.items():
            rows.append({"ticker": ticker, "date": d, score_col: float(s)})
    return pd.DataFrame(rows)


# ─── compute_forward_returns (BUG-009 timing contract) ────────────────────────

class TestComputeForwardReturns:
    def test_output_columns(self):
        prices = _make_prices(["A", "B", "C"], 50)
        result = compute_forward_returns(prices, horizons=[1, 5])
        assert set(result.columns) == {
            "ticker", "score_date", "entry_date", "exit_date",
            "horizon_days", "forward_return", "timing_policy_id",
        }

    def test_both_horizons_present(self):
        prices = _make_prices(["A", "B"], 30)
        result = compute_forward_returns(prices, horizons=[1, 5])
        assert set(result["horizon_days"].unique()) == {1, 5}

    def test_no_nan_forward_returns(self):
        prices = _make_prices(["A", "B", "C"], 30)
        result = compute_forward_returns(prices, horizons=[1, 5])
        assert result["forward_return"].notna().all()

    def test_last_h_dates_absent(self):
        """The last h+1 dates cannot have h-session forward returns (score_date
        needs an entry_date at +1 session AND an exit_date at +1+h sessions)."""
        prices = _make_prices(["A"], 20)
        result = compute_forward_returns(prices, horizons=[5])
        all_dates = sorted(prices["date"].unique())
        result_score_dates = set(result[result["horizon_days"] == 5]["score_date"].unique())
        for d in all_dates[-6:]:
            assert d not in result_score_dates

    def test_score_date_strictly_before_entry_date(self):
        """BUG-009: score_date < entry_date is mandatory."""
        prices = _make_prices(["A", "B"], 30)
        result = compute_forward_returns(prices, horizons=[1, 5])
        assert (result["score_date"] < result["entry_date"]).all()
        assert (result["entry_date"] < result["exit_date"]).all()

    def test_entry_date_is_one_session_after_score_date_under_baseline_policy(self):
        dates = _business_dates(date(2022, 1, 3), 10)
        prices = pd.DataFrame(
            [{"ticker": "X", "date": d, "close": 100.0 + i} for i, d in enumerate(dates)]
        )
        result = compute_forward_returns(prices, horizons=[1])
        row = result[result["score_date"] == dates[0]].iloc[0]
        assert row["entry_date"] == dates[1]
        assert row["exit_date"] == dates[2]

    def test_correct_return_magnitude(self):
        """Manual check: 1-session forward return = close[entry+1]/close[entry] - 1,
        where entry = score_date + 1 session (BUG-009 baseline t+1 convention)."""
        dates = _business_dates(date(2022, 1, 3), 5)
        prices_data = [{"ticker": "X", "date": d, "close": float(100 + i * 10)} for i, d in enumerate(dates)]
        prices = pd.DataFrame(prices_data)
        result = compute_forward_returns(prices, horizons=[1])
        row = result[result["score_date"] == dates[0]].iloc[0]
        # entry = dates[1] (close 110), exit = dates[2] (close 120)
        expected = 120.0 / 110.0 - 1.0
        assert abs(row["forward_return"] - expected) < 1e-9

    def test_timing_policy_id_recorded(self):
        prices = _make_prices(["A", "B"], 20)
        result = compute_forward_returns(prices, horizons=[1])
        assert (result["timing_policy_id"] == DEFAULT_TIMING_POLICY.policy_id).all()

    def test_custom_timing_policy_longer_lag(self):
        """A 2-session execution lag pushes entry_date two sessions past score_date."""
        dates = _business_dates(date(2022, 1, 3), 10)
        prices = pd.DataFrame(
            [{"ticker": "X", "date": d, "close": 100.0 + i} for i, d in enumerate(dates)]
        )
        policy = TimingPolicy(policy_id="t_plus_2_close_v1", execution_lag_sessions=2)
        result = compute_forward_returns(prices, horizons=[1], timing_policy=policy)
        row = result[result["score_date"] == dates[0]].iloc[0]
        assert row["entry_date"] == dates[2]
        assert row["exit_date"] == dates[3]

    def test_zero_lag_policy_rejected_at_construction(self):
        """execution_lag_sessions >= 1 is enforced: score_date < entry_date is mandatory."""
        with pytest.raises(SameDateScoreError):
            TimingPolicy(policy_id="bad", execution_lag_sessions=0)

    def test_holiday_gap_on_one_ticker_does_not_shift_another_tickers_horizon(self):
        """A missing bar for ticker B must not shift ticker A's row-derived horizon."""
        dates_a = _business_dates(date(2022, 1, 3), 10)
        dates_b = [d for i, d in enumerate(dates_a) if i != 5]  # B is missing one session
        prices = pd.DataFrame(
            [{"ticker": "A", "date": d, "close": 100.0 + i} for i, d in enumerate(dates_a)]
            + [{"ticker": "B", "date": d, "close": 200.0 + i} for i, d in enumerate(dates_b)]
        )
        result = compute_forward_returns(prices, horizons=[1])
        row_a = result[(result["ticker"] == "A") & (result["score_date"] == dates_a[0])].iloc[0]
        # A's own calendar is unaffected by B's missing session: entry/exit are
        # still A's immediate next two sessions.
        assert row_a["entry_date"] == dates_a[1]
        assert row_a["exit_date"] == dates_a[2]

    def test_empty_horizons_returns_empty(self):
        prices = _make_prices(["A"], 10)
        result = compute_forward_returns(prices, horizons=[])
        assert result.empty

    def test_missing_column_raises(self):
        prices = _make_prices(["A"], 10).drop(columns=["close"])
        with pytest.raises(ValueError, match="missing columns"):
            compute_forward_returns(prices, horizons=[1])


# ─── compute_realized_forward_returns_as_of (adversarial review round 4) ──────

class TestComputeRealizedForwardReturnsAsOf:
    """BUG-009 section 2.3/2.4 adversarial-review round 4: a single global
    boundary cutoff (used for both the score and realized-return series in
    an earlier draft) is provably safe for the SCORE series (BUG-071's
    ratio-cancellation argument) but NOT for realized returns: an action
    not yet knowable at an EARLIER exit_date's own cutoff, but knowable by
    a later shared boundary, must not adjust that earlier exit's return.
    """

    def _dates(self, n: int = 5) -> list[date]:
        return _business_dates(date(2022, 1, 3), n)

    def test_action_unknown_at_earlier_exit_does_not_adjust_it(self):
        d = self._dates(5)  # d0..d4
        prices = pd.DataFrame(
            [
                {"ticker": "X", "date": d[0], "close": 100.0},
                {"ticker": "X", "date": d[1], "close": 100.0},  # entry (score_date=d0)
                {"ticker": "X", "date": d[2], "close": 100.0},  # split ex_date
                {"ticker": "X", "date": d[3], "close": 50.0},   # exit0 (score_date=d0, h=2)
                {"ticker": "X", "date": d[4], "close": 51.0},   # exit1 (score_date=d1, h=2)
            ]
        )
        # Split known the evening AFTER exit0's own session-close cutoff
        # (21:00 UTC on d3) but before exit1's cutoff (21:00 UTC on d4) —
        # exactly the gap a single shared boundary cutoff would blur.
        known_at = datetime(d[3].year, d[3].month, d[3].day, 22, 0, tzinfo=timezone.utc)
        corporate_actions = pd.DataFrame(
            [
                {
                    "ticker": "X",
                    "ex_date": d[2],
                    "action_type": "split",
                    "value": 2.0,
                    "known_at": known_at,
                    "source_version": "test",
                }
            ]
        )

        result = compute_realized_forward_returns_as_of(prices, corporate_actions, horizons=[2])

        row0 = result[result["score_date"] == d[0]].iloc[0]  # entry=d1, exit=d3
        row1 = result[result["score_date"] == d[1]].iloc[0]  # entry=d2, exit=d4

        # Pair 0: entry(d1) < ex_date(d2) < exit(d3) — the split WOULD
        # distort this ratio if incorrectly included. It must not be: the
        # action was not yet known by exit0's own cutoff, so pair 0's
        # return must equal the raw, unadjusted ratio.
        expected_raw_ratio_0 = 50.0 / 100.0 - 1.0
        assert abs(row0["forward_return"] - expected_raw_ratio_0) < 1e-9

        # Pair 1 (entry=d2=ex_date, exit=d4): structurally unaffected by
        # this action regardless of inclusion (neither endpoint is
        # strictly before ex_date) — a control proving the test isolates
        # pair 0 specifically, not some unrelated difference.
        expected_raw_ratio_1 = 51.0 / 100.0 - 1.0
        assert abs(row1["forward_return"] - expected_raw_ratio_1) < 1e-9

    def test_action_known_by_its_own_exit_is_applied(self):
        """Sanity check: when known_at IS before the relevant exit's own
        cutoff, the adjustment must still apply (this is not a
        never-adjust regression)."""
        d = self._dates(4)
        prices = pd.DataFrame(
            [
                {"ticker": "X", "date": d[0], "close": 100.0},
                {"ticker": "X", "date": d[1], "close": 100.0},  # entry
                {"ticker": "X", "date": d[2], "close": 100.0},  # split ex_date
                {"ticker": "X", "date": d[3], "close": 50.0},   # exit
            ]
        )
        known_at = datetime(d[2].year, d[2].month, d[2].day, 21, 0, tzinfo=timezone.utc)
        corporate_actions = pd.DataFrame(
            [
                {
                    "ticker": "X",
                    "ex_date": d[2],
                    "action_type": "split",
                    "value": 2.0,
                    "known_at": known_at,
                    "source_version": "test",
                }
            ]
        )

        result = compute_realized_forward_returns_as_of(prices, corporate_actions, horizons=[2])
        row0 = result[result["score_date"] == d[0]].iloc[0]
        # entry(d1) gets adjusted (100 * 0.5 = 50), exit(d3) does not (50).
        # Adjusted ratio = 50/50 - 1 = 0.0, not the raw -0.5.
        assert abs(row0["forward_return"] - 0.0) < 1e-9

    def test_consecutive_exit_dates_with_no_new_actions_share_one_panel_build(self, monkeypatch):
        """Adversarial-review round 7 perf fix: many consecutive exit dates
        share an identical eligible-action set (no new action becomes
        knowable between them) and must reuse ONE cached adjusted panel
        instead of rebuilding it per exit date."""
        import data.normalization.corporate_actions as corp_actions_module

        d = self._dates(10)  # d0..d9, no corporate actions at all
        prices = pd.DataFrame(
            [{"ticker": "X", "date": dd, "close": 100.0 + i} for i, dd in enumerate(d)]
        )
        corporate_actions = pd.DataFrame(
            columns=["ticker", "ex_date", "action_type", "value", "known_at", "source_version"]
        )

        call_count = {"n": 0}
        real_builder = corp_actions_module.build_realized_total_return_as_of

        def _counting_builder(*args, **kwargs):
            call_count["n"] += 1
            return real_builder(*args, **kwargs)

        monkeypatch.setattr(corp_actions_module, "build_realized_total_return_as_of", _counting_builder)

        result = compute_realized_forward_returns_as_of(prices, corporate_actions, horizons=[1, 2, 3])

        # Several distinct exit dates are produced (one per score_date x
        # horizon combination), but with zero corporate actions the
        # eligible-action set is identical (empty) for every one of them —
        # the panel must be built exactly once, not once per exit date.
        n_distinct_exit_dates = result["exit_date"].nunique()
        assert n_distinct_exit_dates > 1, "test needs more than one distinct exit date to be meaningful"
        assert call_count["n"] == 1

    def test_new_action_forces_a_fresh_panel_build(self, monkeypatch):
        """Cache correctness guard: once a new action becomes eligible for
        a later exit date, that date's eligible-action set differs from
        earlier dates, so it must trigger a fresh (correct) panel build
        rather than reusing the earlier, now-stale cache entry."""
        import data.normalization.corporate_actions as corp_actions_module

        d = self._dates(10)
        prices = pd.DataFrame(
            [{"ticker": "X", "date": dd, "close": 100.0 + i} for i, dd in enumerate(d)]
        )
        # Known well before d[9]'s cutoff but after d[2]'s -- splits the
        # exit dates into (at least) two distinct eligible-action groups.
        known_at = datetime(d[3].year, d[3].month, d[3].day, 21, 0, tzinfo=timezone.utc)
        corporate_actions = pd.DataFrame(
            [
                {
                    "ticker": "X",
                    "ex_date": d[3],
                    "action_type": "split",
                    "value": 2.0,
                    "known_at": known_at,
                    "source_version": "test",
                }
            ]
        )

        call_count = {"n": 0}
        real_builder = corp_actions_module.build_realized_total_return_as_of

        def _counting_builder(*args, **kwargs):
            call_count["n"] += 1
            return real_builder(*args, **kwargs)

        monkeypatch.setattr(corp_actions_module, "build_realized_total_return_as_of", _counting_builder)

        compute_realized_forward_returns_as_of(prices, corporate_actions, horizons=[1, 2, 3])

        # At least two distinct eligible-action groups (before/after the
        # action becomes eligible) -- more than one build, but still far
        # fewer than one per distinct exit date.
        assert call_count["n"] >= 2

    def test_output_columns_match_return_series_columns(self):
        d = self._dates(4)
        prices = pd.DataFrame(
            [{"ticker": "X", "date": dd, "close": 100.0 + i} for i, dd in enumerate(d)]
        )
        corporate_actions = pd.DataFrame(
            columns=["ticker", "ex_date", "action_type", "value", "known_at", "source_version"]
        )
        result = compute_realized_forward_returns_as_of(prices, corporate_actions, horizons=[1])
        assert list(result.columns) == [
            "ticker", "score_date", "entry_date", "exit_date",
            "horizon_days", "forward_return", "timing_policy_id",
        ]

    def test_feeds_compute_ic_series_via_precomputed_forward_returns(self):
        d = self._dates(6)
        tickers = ["A", "B", "C", "D", "E", "F"]
        rows = []
        for i, t in enumerate(tickers):
            for j, dd in enumerate(d):
                rows.append({"ticker": t, "date": dd, "close": 100.0 + i + j})
        prices = pd.DataFrame(rows)
        corporate_actions = pd.DataFrame(
            columns=["ticker", "ex_date", "action_type", "value", "known_at", "source_version"]
        )
        fwd = compute_realized_forward_returns_as_of(prices, corporate_actions, horizons=[1])

        scores = pd.DataFrame(
            [{"ticker": t, "date": d[0], "score": float(i)} for i, t in enumerate(tickers)]
        )
        result = compute_ic_series(
            scores, None, score_col="score", horizons=[1], precomputed_forward_returns=fwd
        )
        assert not result.empty
        assert set(result.columns) == {
            "score_date", "horizon_days", "ic", "rank_ic", "n_obs", "timing_policy_id",
        }

    def test_precomputed_forward_returns_missing_columns_raises(self):
        scores = pd.DataFrame({"ticker": ["A"], "date": [date(2022, 1, 3)], "score": [1.0]})
        bad_fwd = pd.DataFrame({"ticker": ["A"], "score_date": [date(2022, 1, 3)]})
        with pytest.raises(ValueError, match="missing columns"):
            compute_ic_series(
                scores, None, score_col="score", horizons=[1], precomputed_forward_returns=bad_fwd
            )

    def test_precomputed_forward_returns_stamps_frames_own_policy_id(self):
        """BUG-009 round-5 P2: output must reflect the timing_policy_id
        ACTUALLY present in the precomputed frame, not the timing_policy
        argument's default -- especially when the caller didn't pass a
        matching timing_policy argument at all."""
        d = self._dates(6)
        tickers = ["A", "B", "C", "D", "E", "F"]
        fwd_rows = []
        for i, t in enumerate(tickers):
            fwd_rows.append({
                "ticker": t,
                "score_date": d[0],
                "entry_date": d[1],
                "exit_date": d[2],
                "horizon_days": 1,
                "forward_return": 0.01 * i,
                "timing_policy_id": "custom_policy_v9",
            })
        fwd = pd.DataFrame(fwd_rows)
        scores = pd.DataFrame(
            [{"ticker": t, "date": d[0], "score": float(i)} for i, t in enumerate(tickers)]
        )

        # Note: no timing_policy argument passed -- default is
        # DEFAULT_TIMING_POLICY ("t_plus_1_close_v1"), which must NOT leak
        # into the output when a precomputed frame is supplied.
        result = compute_ic_series(
            scores, None, score_col="score", horizons=[1], precomputed_forward_returns=fwd
        )
        assert not result.empty
        assert (result["timing_policy_id"] == "custom_policy_v9").all()
        assert not (result["timing_policy_id"] == DEFAULT_TIMING_POLICY.policy_id).any()

    def test_precomputed_forward_returns_mixed_policy_ids_rejected(self):
        d = self._dates(6)
        tickers = ["A", "B", "C", "D", "E", "F"]
        fwd_rows = []
        for i, t in enumerate(tickers):
            fwd_rows.append({
                "ticker": t,
                "score_date": d[0],
                "entry_date": d[1],
                "exit_date": d[2],
                "horizon_days": 1,
                "forward_return": 0.01 * i,
                # Half the rows claim one policy, half another -- ambiguous.
                "timing_policy_id": "policy_a" if i % 2 == 0 else "policy_b",
            })
        fwd = pd.DataFrame(fwd_rows)
        scores = pd.DataFrame(
            [{"ticker": t, "date": d[0], "score": float(i)} for i, t in enumerate(tickers)]
        )
        with pytest.raises(ValueError, match="timing_policy_id"):
            compute_ic_series(
                scores, None, score_col="score", horizons=[1], precomputed_forward_returns=fwd
            )

    def test_precomputed_forward_returns_same_close_score_date_rejected(self):
        """Adversarial-review round 9 (BUG-009): the precomputed-frame bypass
        skips the internal build_return_series call where score_date <
        entry_date is normally enforced. A legacy or hand-built same-close
        frame (entry_date == score_date, reproducing the ORIGINAL BUG-009
        lookahead) must be rejected here too, not silently accepted just
        because it arrived via this entry point instead of the normal one."""
        d = self._dates(6)
        tickers = ["A", "B", "C", "D", "E", "F"]
        fwd_rows = []
        for i, t in enumerate(tickers):
            fwd_rows.append({
                "ticker": t,
                "score_date": d[0],
                "entry_date": d[0],  # same-close lookahead: entry == score
                "exit_date": d[1],
                "horizon_days": 1,
                "forward_return": 0.01 * i,
                "timing_policy_id": "legacy_same_close_v0",
            })
        fwd = pd.DataFrame(fwd_rows)
        scores = pd.DataFrame(
            [{"ticker": t, "date": d[0], "score": float(i)} for i, t in enumerate(tickers)]
        )
        with pytest.raises(SameDateScoreError, match="score_date"):
            compute_ic_series(
                scores, None, score_col="score", horizons=[1], precomputed_forward_returns=fwd
            )

    def test_precomputed_forward_returns_entry_after_exit_rejected(self):
        """entry_date must also be strictly before exit_date -- a
        zero-or-negative-length "forward" return is just as invalid as a
        same-close one, and reject_same_date alone only checks the
        score_date/entry_date pair."""
        d = self._dates(6)
        tickers = ["A", "B", "C", "D", "E", "F"]
        fwd_rows = []
        for i, t in enumerate(tickers):
            fwd_rows.append({
                "ticker": t,
                "score_date": d[0],
                "entry_date": d[2],
                "exit_date": d[1],  # exit BEFORE entry
                "horizon_days": 1,
                "forward_return": 0.01 * i,
                "timing_policy_id": "custom_policy_v9",
            })
        fwd = pd.DataFrame(fwd_rows)
        scores = pd.DataFrame(
            [{"ticker": t, "date": d[0], "score": float(i)} for i, t in enumerate(tickers)]
        )
        with pytest.raises(SameDateScoreError, match="exit_date"):
            compute_ic_series(
                scores, None, score_col="score", horizons=[1], precomputed_forward_returns=fwd
            )

    def test_precomputed_forward_returns_valid_ordering_still_passes(self):
        """Sanity: the new validation must not reject a genuinely valid
        precomputed frame (score_date < entry_date < exit_date on every
        row) -- covered implicitly by
        test_feeds_compute_ic_series_via_precomputed_forward_returns, but
        asserted directly here against a hand-built frame rather than one
        built via compute_realized_forward_returns_as_of."""
        d = self._dates(6)
        tickers = ["A", "B", "C", "D", "E", "F"]
        fwd_rows = []
        for i, t in enumerate(tickers):
            fwd_rows.append({
                "ticker": t,
                "score_date": d[0],
                "entry_date": d[1],
                "exit_date": d[2],
                "horizon_days": 1,
                "forward_return": 0.01 * i,
                "timing_policy_id": "custom_policy_v9",
            })
        fwd = pd.DataFrame(fwd_rows)
        scores = pd.DataFrame(
            [{"ticker": t, "date": d[0], "score": float(i)} for i, t in enumerate(tickers)]
        )
        result = compute_ic_series(
            scores, None, score_col="score", horizons=[1], precomputed_forward_returns=fwd
        )
        assert not result.empty


# ─── compute_ic_series ────────────────────────────────────────────────────────

class TestComputeICSeries:
    def test_output_columns(self):
        prices = _make_prices(["A", "B", "C", "D", "E", "F"], 100)
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1, 5])
        assert set(result.columns) == {
            "score_date", "horizon_days", "ic", "rank_ic", "n_obs", "timing_policy_id",
        }

    def test_timing_policy_id_recorded(self):
        prices = _make_prices(["A", "B", "C", "D", "E", "F"], 60)
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1])
        assert (result["timing_policy_id"] == DEFAULT_TIMING_POLICY.policy_id).all()

    def test_score_at_close_t_cannot_receive_close_t_return(self):
        """BUG-009 core acceptance test: a score using close[t] must not be
        correlated against any component of the t close-to-close return.

        Constructed so the t -> t+1 return (the historical same-close bug)
        is a large, perfectly-known positive jump; if the fix regressed to
        crediting that jump to score_date's own IC, this deterministic jump
        would appear as return magnitude in the score_date row. Instead the
        forward_return actually used is entry(t+1) -> exit(t+2), which is
        flat, so IC on the jump date must be near zero / undefined — not the
        artificially large value the same-close bug would have produced.
        """
        dates = _business_dates(date(2022, 1, 3), 10)
        tickers = ["A", "B", "C", "D", "E", "F"]
        rows = []
        for i, ticker in enumerate(tickers):
            for j, d in enumerate(dates):
                # A deterministic, huge jump from dates[0] -> dates[1] whose
                # magnitude is monotonically ticker-ranked (would produce a
                # strong same-close IC=1.0 if the bug were present), then
                # flat afterward (post-jump returns carry no signal).
                if j == 0:
                    close = 100.0 * (i + 1)
                else:
                    close = 100.0 * (i + 1) * 10.0  # jump applied from j=1 onward
                rows.append({"ticker": ticker, "date": d, "close": close})
        prices = pd.DataFrame(rows)

        # Score on dates[0] ranks tickers identically to the jump size —
        # under the same-close bug this would yield IC ~ 1.0 on dates[0].
        scores = pd.DataFrame(
            [{"ticker": t, "date": dates[0], "score": float(i)} for i, t in enumerate(tickers)]
        )
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1])
        # entry = dates[1], exit = dates[2]: both post-jump and IDENTICAL in
        # relative terms (flat), so the forward_return is 0 for every ticker
        # -> constant forward returns -> undefined (NaN) correlation, never 1.0.
        row = result[result["score_date"] == dates[0]]
        if not row.empty:
            assert pd.isna(row.iloc[0]["ic"]) or abs(row.iloc[0]["ic"]) < 1e-6

    def test_entry_and_exit_dates_available_via_forward_returns(self):
        """compute_forward_returns (which compute_ic_series merges on) names
        entry/exit dates explicitly per score_date."""
        prices = _make_prices(["A", "B", "C", "D", "E", "F"], 40)
        fwd = compute_forward_returns(prices, horizons=[1])
        assert {"score_date", "entry_date", "exit_date"}.issubset(set(fwd.columns))
        assert (fwd["score_date"] < fwd["entry_date"]).all()

    def test_both_horizons_present(self):
        prices = _make_prices(list("ABCDEFGHIJ"), 80)
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1, 5])
        assert {1, 5}.issubset(set(result["horizon_days"].unique()))

    def test_ic_in_valid_range(self):
        prices = _make_prices(list("ABCDEFGHIJ"), 80)
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1])
        assert result["ic"].between(-1.0, 1.0).all()
        assert result["rank_ic"].between(-1.0, 1.0).all()

    def test_predictive_scores_yield_positive_ic(self):
        """Scores constructed to predict forward returns should have positive mean IC."""
        prices = _make_prices(list("ABCDEFGHIJKLMNOP"), 300, seed=1)
        scores = _make_predictive_scores(prices, horizon=5, noise=0.3)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[5])
        mean_ic = result[result["horizon_days"] == 5]["ic"].mean()
        assert mean_ic > 0.0, f"Expected positive mean IC for predictive scores, got {mean_ic:.4f}"

    def test_random_scores_near_zero_ic(self):
        """Truly random scores should produce mean IC near 0."""
        prices = _make_prices(list("ABCDEFGHIJKLMNOP"), 400, seed=2)
        scores = _make_scores(prices, seed=99)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[5])
        mean_ic = result["ic"].mean()
        assert abs(mean_ic) < 0.15, f"Expected near-zero mean IC, got {mean_ic:.4f}"

    def test_fewer_than_min_tickers_excluded(self):
        """Dates with fewer than 5 tickers should be excluded from IC series."""
        prices = _make_prices(["A", "B", "C"], 30)  # only 3 tickers
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1])
        assert result.empty

    def test_missing_score_col_raises(self):
        prices = _make_prices(["A", "B"], 20)
        scores = _make_scores(prices)
        with pytest.raises(ValueError, match="missing columns"):
            compute_ic_series(scores, prices, score_col="nonexistent", horizons=[1])

    def test_default_horizons_used(self):
        prices = _make_prices(list("ABCDEFGHIJ"), 120)
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score")
        # At minimum the 1-day horizon should appear (63-day needs more data)
        assert 1 in result["horizon_days"].values


# ─── summarize_ic ─────────────────────────────────────────────────────────────

class TestSummarizeIC:
    def _make_ic_series(self, n_dates: int = 50, horizons: list[int] = None) -> pd.DataFrame:
        if horizons is None:
            horizons = [1, 21]
        rng = np.random.default_rng(0)
        dates = _business_dates(date(2021, 1, 4), n_dates)
        rows = []
        for d in dates:
            for h in horizons:
                rows.append({
                    "score_date": d,
                    "horizon_days": h,
                    "ic": float(rng.normal(0.04, 0.10)),
                    "rank_ic": float(rng.normal(0.03, 0.10)),
                    "n_obs": 100,
                })
        return pd.DataFrame(rows)

    def test_output_columns(self):
        ic_series = self._make_ic_series()
        result = summarize_ic(ic_series, factor_name="test_factor")
        expected_cols = {
            "factor_name", "strategy_id", "eval_date", "horizon_days",
            "ic", "rank_ic", "ic_tstat", "ic_ir", "ic_pvalue", "n_observations",
        }
        assert expected_cols.issubset(set(result.columns))

    def test_one_row_per_horizon(self):
        ic_series = self._make_ic_series(horizons=[1, 21])
        result = summarize_ic(ic_series, factor_name="f")
        assert len(result) == 2
        assert set(result["horizon_days"]) == {1, 21}

    def test_timing_policy_id_propagated_from_ic_series(self):
        ic_series = self._make_ic_series()
        ic_series["timing_policy_id"] = "t_plus_1_close_v1"
        result = summarize_ic(ic_series, factor_name="f")
        assert (result["timing_policy_id"] == "t_plus_1_close_v1").all()

    def test_mixed_timing_policy_ids_rejected(self):
        ic_series = self._make_ic_series()
        half = len(ic_series) // 2
        ic_series["timing_policy_id"] = ["policy_a"] * half + ["policy_b"] * (len(ic_series) - half)
        with pytest.raises(ValueError, match="timing_policy_id"):
            summarize_ic(ic_series, factor_name="f")

    def test_missing_timing_policy_id_column_yields_none(self):
        """Backward-compat: ic_series without a timing_policy_id column
        (e.g. a hand-built fixture) still summarizes, with the field null."""
        ic_series = self._make_ic_series()
        result = summarize_ic(ic_series, factor_name="f")
        assert result["timing_policy_id"].isna().all()

    def test_factor_name_propagated(self):
        ic_series = self._make_ic_series()
        result = summarize_ic(ic_series, factor_name="my_factor")
        assert (result["factor_name"] == "my_factor").all()

    def test_eval_date_defaults_to_max(self):
        ic_series = self._make_ic_series(n_dates=30)
        max_date = ic_series["score_date"].max()
        result = summarize_ic(ic_series, factor_name="f")
        assert (result["eval_date"] == max_date).all()

    def test_eval_date_can_be_overridden(self):
        ic_series = self._make_ic_series()
        custom_date = date(2025, 1, 1)
        result = summarize_ic(ic_series, factor_name="f", eval_date=custom_date)
        assert (result["eval_date"] == custom_date).all()

    def test_positive_mean_ic_yields_positive_tstat(self):
        """A consistently positive IC time series should have a positive t-stat."""
        dates = _business_dates(date(2020, 1, 2), 60)
        ic_series = pd.DataFrame({
            "score_date": dates,
            "horizon_days": 21,
            "ic": [0.05] * 60,
            "rank_ic": [0.04] * 60,
            "n_obs": 200,
        })
        result = summarize_ic(ic_series, factor_name="f")
        assert result.iloc[0]["ic_tstat"] > 0

    def test_overlapping_horizon_uses_hac_inference(self):
        """Serially correlated IC should get a smaller HAC t-stat than naive."""
        rng = np.random.default_rng(42)
        innovations = rng.normal(0.0, 0.02, 120)
        values = np.empty(120)
        values[0] = 0.04 + innovations[0]
        for i in range(1, len(values)):
            values[i] = 0.04 + 0.9 * (values[i - 1] - 0.04) + innovations[i]
        dates = _business_dates(date(2020, 1, 2), len(values))
        ic_series = pd.DataFrame({
            "score_date": dates,
            "horizon_days": 21,
            "ic": values,
            "rank_ic": values,
            "n_obs": 200,
        })

        result = summarize_ic(ic_series, factor_name="f")
        naive_tstat = values.mean() / (values.std(ddof=1) / np.sqrt(len(values)))

        assert result.iloc[0]["ic_tstat"] < naive_tstat

    def test_empty_input_returns_empty(self):
        result = summarize_ic(pd.DataFrame(columns=["date", "horizon_days", "ic", "rank_ic", "n_obs"]), "f")
        assert result.empty

    def test_insufficient_dates_excluded(self):
        """Horizons with fewer than _MIN_IC_DATES_FOR_TSTAT obs are excluded."""
        dates = _business_dates(date(2021, 1, 4), 5)  # only 5 dates
        ic_series = pd.DataFrame({
            "score_date": dates,
            "horizon_days": 21,
            "ic": [0.04] * 5,
            "rank_ic": [0.03] * 5,
            "n_obs": 50,
        })
        result = summarize_ic(ic_series, factor_name="f")
        assert result.empty


# ─── multiple_testing_correction ─────────────────────────────────────────────

class TestMultipleTestingCorrection:
    def _make_summaries(self, pvalues: list[float]) -> pd.DataFrame:
        return pd.DataFrame({
            "factor_name": [f"f{i}" for i in range(len(pvalues))],
            "ic_pvalue": pvalues,
        })

    def test_adds_corrected_pvalue_and_significant_columns(self):
        summaries = self._make_summaries([0.001, 0.01, 0.05, 0.5])
        result = multiple_testing_correction(summaries)
        assert "corrected_pvalue" in result.columns
        assert "significant" in result.columns

    def test_very_small_pvalue_significant(self):
        """A p-value of 0.0001 should survive any reasonable multiple testing correction."""
        summaries = self._make_summaries([0.0001, 0.3, 0.5, 0.7, 0.9])
        result = multiple_testing_correction(summaries, method="bhy", alpha=0.05)
        assert result[result["ic_pvalue"] == 0.0001]["significant"].iloc[0]

    def test_large_pvalues_not_significant(self):
        summaries = self._make_summaries([0.5, 0.6, 0.7, 0.8, 0.9])
        result = multiple_testing_correction(summaries)
        assert not result["significant"].any()

    def test_bh_and_bhy_both_work(self):
        summaries = self._make_summaries([0.001, 0.01, 0.05, 0.5])
        for method in ("bh", "bhy"):
            result = multiple_testing_correction(summaries, method=method)
            assert "significant" in result.columns

    def test_invalid_method_raises(self):
        summaries = self._make_summaries([0.05])
        with pytest.raises(ValueError, match="method"):
            multiple_testing_correction(summaries, method="bonferroni")

    def test_missing_pvalue_column_raises(self):
        with pytest.raises(ValueError, match="ic_pvalue"):
            multiple_testing_correction(pd.DataFrame({"ic": [0.04]}))

    def test_bhy_more_conservative_than_bh(self):
        """BHY should reject fewer or equal hypotheses compared to BH."""
        summaries = self._make_summaries([0.001, 0.005, 0.01, 0.04, 0.06, 0.2])
        bh = multiple_testing_correction(summaries, method="bh")
        bhy = multiple_testing_correction(summaries, method="bhy")
        assert bh["significant"].sum() >= bhy["significant"].sum()


# ─── chronological_split ──────────────────────────────────────────────────────

class TestChronologicalSplit:
    def _make_scores(self, n_dates: int) -> pd.DataFrame:
        dates = _business_dates(date(2021, 1, 4), n_dates)
        return pd.DataFrame({
            "ticker": ["A"] * n_dates,
            "date": dates,
            "score": range(n_dates),
        })

    def test_no_overlap_between_train_and_val(self):
        scores = self._make_scores(100)
        train, val = chronological_split(scores, train_fraction=0.7)
        train_dates = set(train["date"])
        val_dates = set(val["date"])
        assert train_dates.isdisjoint(val_dates)

    def test_train_val_cover_all_dates(self):
        scores = self._make_scores(100)
        train, val = chronological_split(scores, train_fraction=0.7)
        all_dates = set(scores["date"])
        assert train["date"].tolist() or val["date"].tolist()
        assert set(train["date"]) | set(val["date"]) == all_dates

    def test_train_precedes_val(self):
        scores = self._make_scores(100)
        train, val = chronological_split(scores, train_fraction=0.7)
        assert max(train["date"]) < min(val["date"])

    def test_approximate_split_fraction(self):
        scores = self._make_scores(100)
        train, val = chronological_split(scores, train_fraction=0.7)
        n_total = len(scores["date"].unique())
        n_train = len(train["date"].unique())
        assert abs(n_train / n_total - 0.7) < 0.05

    def test_fraction_one_puts_everything_in_train(self):
        scores = self._make_scores(20)
        train, val = chronological_split(scores, train_fraction=1.0)
        assert len(train) == len(scores)
        assert len(val) == 0

    def test_fraction_zero_raises(self):
        scores = self._make_scores(20)
        with pytest.raises(ValueError, match="train_fraction"):
            chronological_split(scores, train_fraction=0.0)

    def test_empty_input_returns_two_empty_dataframes(self):
        empty = pd.DataFrame(columns=["ticker", "date", "score"])
        train, val = chronological_split(empty)
        assert train.empty
        assert val.empty


# ─── Edge case: NaN scores and constant scores ────────────────────────────────

class TestICEdgeCases:
    def test_nan_scores_excluded_from_ic(self):
        """Tickers with NaN scores on a date are excluded from that date's IC."""
        prices = _make_prices(list("ABCDEFGHIJ"), 80)
        scores = _make_scores(prices)
        # Introduce NaN scores for half the tickers on all dates
        nan_tickers = list("ABCDE")
        mask = scores["ticker"].isin(nan_tickers)
        scores_with_nan = scores.copy()
        scores_with_nan.loc[mask, "score"] = np.nan

        result_clean = compute_ic_series(scores, prices, score_col="score", horizons=[1])
        result_nan = compute_ic_series(scores_with_nan, prices, score_col="score", horizons=[1])

        # IC series should still be produced using the 5 non-NaN tickers
        assert not result_nan.empty
        # n_obs should be roughly half what the clean version produces
        assert result_nan["n_obs"].mean() < result_clean["n_obs"].mean()

    def test_constant_scores_produce_nan_ic(self):
        """All-identical scores for a date yield NaN IC (undefined correlation)."""
        prices = _make_prices(list("ABCDEFGHIJ"), 80)
        scores = _make_scores(prices)
        # Force all scores to the same value
        scores["score"] = 1.0
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1])
        # IC should be NaN for all dates (constant scores → undefined correlation)
        if not result.empty:
            assert result["ic"].isna().all()

    def test_one_sided_tstat_positive_for_good_factor(self):
        """A consistently positive IC time series should have p-value < 0.05 (one-sided)."""
        dates = _business_dates(date(2020, 1, 2), 60)
        ic_series = pd.DataFrame({
            "score_date": dates,
            "horizon_days": 21,
            "ic": [0.06] * 60,
            "rank_ic": [0.05] * 60,
            "n_obs": 200,
        })
        result = summarize_ic(ic_series, factor_name="f")
        assert result.iloc[0]["ic_pvalue"] < 0.05


# ─── compute_factor_turnover ──────────────────────────────────────────────────

from signals.research.ic import compute_factor_turnover, rolling_ic_summary, compute_ic_ir_weights


class TestComputeFactorTurnover:
    def _stable_scores(self, n_tickers: int = 20, n_dates: int = 60) -> pd.DataFrame:
        """Scores that are identical across all dates → rank autocorr ≈ 1."""
        dates = _business_dates(date(2022, 1, 3), n_dates)
        tickers = [f"T{i:02d}" for i in range(n_tickers)]
        rows = []
        for d in dates:
            for i, t in enumerate(tickers):
                rows.append({"ticker": t, "date": d, "score": float(i)})
        return pd.DataFrame(rows)

    def _random_scores(self, n_tickers: int = 20, n_dates: int = 60, seed: int = 0) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        dates = _business_dates(date(2022, 1, 3), n_dates)
        tickers = [f"T{i:02d}" for i in range(n_tickers)]
        rows = []
        for d in dates:
            for t in tickers:
                rows.append({"ticker": t, "date": d, "score": float(rng.standard_normal(1)[0])})
        return pd.DataFrame(rows)

    def test_output_columns(self):
        scores = self._stable_scores()
        result = compute_factor_turnover(scores, "score")
        assert set(result.columns) >= {"score_date", "lag_date", "lag_calendar_days", "rank_autocorrelation", "ticker_count"}

    def test_stable_scores_yield_high_autocorrelation(self):
        """Constant rankings → autocorrelation should be 1."""
        scores = self._stable_scores()
        result = compute_factor_turnover(scores, "score", rebalance_days=21)
        assert not result.empty
        assert (result["rank_autocorrelation"] > 0.99).all()

    def test_random_scores_yield_low_autocorrelation(self):
        """i.i.d. scores → autocorrelation should be near 0."""
        scores = self._random_scores(n_tickers=50, n_dates=120)
        result = compute_factor_turnover(scores, "score", rebalance_days=21)
        assert not result.empty
        assert result["rank_autocorrelation"].mean() < 0.3

    def test_ticker_count_reasonable(self):
        scores = self._stable_scores(n_tickers=15)
        result = compute_factor_turnover(scores, "score", rebalance_days=5)
        assert (result["ticker_count"] == 15).all()

    def test_no_rows_when_insufficient_history(self):
        """If all dates are within rebalance_days of start, no rows produced."""
        dates = _business_dates(date(2022, 1, 3), 5)
        rows = [{"ticker": "A", "date": d, "score": 1.0} for d in dates]
        scores = pd.DataFrame(rows)
        result = compute_factor_turnover(scores, "score", rebalance_days=30)
        assert result.empty

    def test_lag_date_precedes_score_date(self):
        scores = self._stable_scores()
        result = compute_factor_turnover(scores, "score", rebalance_days=21)
        assert (result["lag_date"] < result["score_date"]).all()

    def test_missing_score_col_raises(self):
        scores = self._stable_scores()
        with pytest.raises(ValueError):
            compute_factor_turnover(scores, "nonexistent_col")


# ─── rolling_ic_summary ───────────────────────────────────────────────────────

class TestRollingICSummary:
    def _make_ic_series(self, n_dates: int = 100, ic_val: float = 0.05) -> pd.DataFrame:
        dates = _business_dates(date(2020, 1, 2), n_dates)
        return pd.DataFrame({
            "score_date": dates * 2,
            "horizon_days": [21] * n_dates + [63] * n_dates,
            "ic": [ic_val] * n_dates + [ic_val * 0.8] * n_dates,
            "rank_ic": [ic_val * 0.9] * n_dates + [ic_val * 0.7] * n_dates,
            "n_obs": 200,
        })

    def test_output_columns(self):
        ic = self._make_ic_series()
        result = rolling_ic_summary(ic)
        expected = {"score_date", "horizon_days", "ic_mean", "rank_ic_mean",
                    "ic_std", "ic_ir", "hit_rate", "n_dates"}
        assert expected <= set(result.columns)

    def test_both_horizons_present(self):
        ic = self._make_ic_series()
        result = rolling_ic_summary(ic, trailing_dates=30)
        assert set(result["horizon_days"].unique()) == {21, 63}

    def test_window_size_capped_at_trailing_dates(self):
        ic = self._make_ic_series(n_dates=100)
        result = rolling_ic_summary(ic, trailing_dates=30)
        h21 = result[result["horizon_days"] == 21]
        # After enough dates have passed, each window is exactly trailing_dates
        assert (h21["n_dates"] <= 30).all()

    def test_constant_ic_yields_correct_mean(self):
        ic = self._make_ic_series(ic_val=0.06)
        result = rolling_ic_summary(ic, trailing_dates=252)
        h21 = result[result["horizon_days"] == 21]
        assert not h21.empty
        assert abs(h21["ic_mean"].iloc[-1] - 0.06) < 1e-9

    def test_positive_ic_yields_hit_rate_one(self):
        ic = self._make_ic_series(ic_val=0.06)
        result = rolling_ic_summary(ic)
        assert (result["hit_rate"] == 1.0).all()

    def test_empty_input_returns_empty(self):
        result = rolling_ic_summary(pd.DataFrame(
            columns=["score_date", "horizon_days", "ic", "rank_ic"]
        ))
        assert result.empty

    def test_insufficient_data_excluded(self):
        """Windows with fewer than min_dates rows should be dropped."""
        dates = _business_dates(date(2022, 1, 3), 10)
        ic = pd.DataFrame({
            "score_date": dates,
            "horizon_days": 21,
            "ic": [0.05] * 10,
            "rank_ic": [0.04] * 10,
        })
        result = rolling_ic_summary(ic, min_dates=30)
        assert result.empty

    def test_missing_column_raises(self):
        ic = pd.DataFrame({"score_date": [date(2022, 1, 3)], "horizon_days": [21], "ic": [0.05]})
        with pytest.raises(ValueError, match="missing columns"):
            rolling_ic_summary(ic)


# ─── compute_ic_ir_weights ────────────────────────────────────────────────────

class TestComputeICIRWeights:
    def _make_summary(self, ic_ir: float, horizon: int = 21) -> pd.DataFrame:
        return pd.DataFrame({
            "horizon_days": [horizon],
            "ic_ir": [ic_ir],
            "ic": [ic_ir * 0.05],
        })

    def test_weights_sum_to_one(self):
        summaries = {
            "momentum": self._make_summary(0.5),
            "lowvol": self._make_summary(0.3),
            "value": self._make_summary(0.2),
        }
        weights = compute_ic_ir_weights(summaries)
        assert abs(sum(weights.values()) - 1.0) < 1e-9

    def test_higher_ic_ir_gets_more_weight(self):
        summaries = {
            "momentum": self._make_summary(1.0),
            "lowvol": self._make_summary(0.5),
        }
        weights = compute_ic_ir_weights(summaries, shrinkage=0.0)
        assert weights["momentum"] > weights["lowvol"]

    def test_negative_ic_ir_zeroed(self):
        summaries = {
            "good": self._make_summary(0.5),
            "bad": self._make_summary(-0.3),
        }
        weights = compute_ic_ir_weights(summaries, shrinkage=0.0)
        assert weights["bad"] == 0.0
        assert abs(weights["good"] - 1.0) < 1e-9

    def test_full_shrinkage_yields_equal_weight(self):
        summaries = {
            "momentum": self._make_summary(1.0),
            "lowvol": self._make_summary(0.1),
        }
        weights = compute_ic_ir_weights(summaries, shrinkage=1.0)
        assert abs(weights["momentum"] - 0.5) < 1e-9
        assert abs(weights["lowvol"] - 0.5) < 1e-9

    def test_max_weight_cap_enforced(self):
        summaries = {
            "dominant": self._make_summary(10.0),
            "weak": self._make_summary(0.01),
        }
        weights = compute_ic_ir_weights(summaries, shrinkage=0.0, max_weight=0.6)
        assert weights["dominant"] <= 0.6 + 1e-9

    def test_all_zero_ic_ir_falls_back_to_equal_weight(self):
        summaries = {
            "a": self._make_summary(-1.0),
            "b": self._make_summary(-0.5),
        }
        weights = compute_ic_ir_weights(summaries)
        assert abs(weights["a"] - 0.5) < 1e-9
        assert abs(weights["b"] - 0.5) < 1e-9

    def test_empty_input_returns_empty(self):
        result = compute_ic_ir_weights({})
        assert result == {}

    def test_invalid_shrinkage_raises(self):
        with pytest.raises(ValueError, match="shrinkage"):
            compute_ic_ir_weights({"a": self._make_summary(0.5)}, shrinkage=1.5)

    def test_infeasible_max_weight_raises(self):
        """max_weight * n_factors < 1.0 is infeasible and should raise."""
        summaries = {
            "a": self._make_summary(0.5),
            "b": self._make_summary(0.4),
            "c": self._make_summary(0.3),
        }
        with pytest.raises(ValueError, match="infeasible"):
            compute_ic_ir_weights(summaries, max_weight=0.2)  # 0.2 * 3 = 0.6 < 1.0

    def test_negative_min_ic_ir_raises(self):
        with pytest.raises(ValueError, match="min_ic_ir"):
            compute_ic_ir_weights({"a": self._make_summary(0.5)}, min_ic_ir=-0.1)

    def test_lag_calendar_days_column_present(self):
        scores = TestComputeFactorTurnover()._stable_scores()
        result = compute_factor_turnover(scores, "score", rebalance_days=21)
        assert "lag_calendar_days" in result.columns
        assert (result["lag_calendar_days"] >= 21).all()
