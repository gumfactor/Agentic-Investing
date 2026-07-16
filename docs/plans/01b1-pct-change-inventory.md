# 01B-1 — `pct_change()` call-site inventory (BUG-010)

Authoritative source: `docs/plans/01b-research-validity-design.md` §3
("Missing-data return policy"). Scope of this document is BUG-010 only —
`signals.indicators._price_utils.daily_return` plus every production
price-return path that differences a price/NAV series with
`pandas.pct_change`. BUG-008 (PIT universe) and BUG-009 (timing contract) are
separate roadmap items and are not touched here.

`rg "\.pct_change\(" --type py` (excluding `tests/`, this document, and
`bugs.md`) finds **33** production call sites as of this inventory, matching
the roadmap task's scope fact. All 33 are in scope; none is exempted. The
repository guard test `tests/test_pct_change_guard.py` enforces this list
mechanically going forward — see that file's `_DOCUMENTED_EXCEPTIONS` for the
(currently empty) exception registry.

## Shared helper

`signals/indicators/_price_utils.py` gained:

- `daily_return(wide)` — the sanctioned way to turn a wide price matrix into a
  daily return matrix inside `signals/indicators/*`. Calls
  `wide.pct_change(fill_method=None)`, and additionally validates that every
  *present* (non-NaN) price is finite and strictly positive (raises
  `ValueError` otherwise — a non-positive/infinite price is a data defect, not
  a "missing" observation, so it fails closed rather than silently becoming a
  NaN return).
- `rolling_valid_count(returns, window)` — count of non-missing returns in
  each trailing window; used to gate statistics where a missing return does
  not automatically propagate as NaN through the downstream arithmetic
  (cumulative sums, sign/boolean masks).
- `require_full_window(value, returns, window)` — suppresses `value` (set to
  NaN) wherever its trailing `window` of `returns` contains a gap. Implements
  the §3.1 default: a lookback of N returns requires N valid, contiguous
  returns, not merely N calendar rows.

Outside `signals/indicators/*` (backtesting, portfolio, reporting), direct
`pct_change(fill_method=None)` calls are used instead of importing the
signals-layer helper, per §3.2 step 2 ("retain direct calls where clearer") —
importing from `signals.indicators` into `backtesting`/`portfolio`/`reporting`
would invert the intended module dependency direction for no benefit, since
those call sites don't need the price-positivity validation (NAV/benchmark
series, or prices already validated upstream by `daily_returns_for_tickers`'s
caller / `covariance.build_covariance`'s NaN-row handling).

Tests: `signals/tests/indicators/test_price_utils.py` (9 tests) cover gap →
NaN (not zero), non-forward-filling across multi-day gaps, rejection of
non-positive/infinite prices, allowance of missing (NaN) prices, and the
`rolling_valid_count` / `require_full_window` gap-gating helpers.

## Rolling-window valid-return policy

§3.1 default: **for a lookback of N returns, the minimum is N valid returns
in the window, and the default is to reject a window spanning a gap.**
Before this change, every migrated indicator used a `min_periods` set to
~70% of its window (e.g. `rolling(21, min_periods=15)`,
`rolling(63, min_periods=44)`, `rolling(252, min_periods=126)`,
`rolling(10, min_periods=7)`). Because `pandas.rolling(...).std()/.mean()/
.cov()/.var()/.apply(raw=True)` only count *non-NaN* observations toward
`min_periods`, setting `min_periods == window` is sufficient by itself to
reject any window containing a gap (a NaN entry reduces the non-null count
below `window`, so the statistic returns NaN) — no additional gap-detection
code is needed for these calls. This inventory therefore raises every
directly-return-derived `min_periods` constant to equal its window, for both
the primary daily-return rolling call and any outer rolling call built
directly on that return series (e.g. `rolling_beta`'s `cov`/`var`, or a
`rolling(...).std()` computed from another rolling `.std()` output).

Two categories of call needed a different treatment because NaN does **not**
propagate automatically through the downstream arithmetic, so raising
`min_periods` alone would not detect a gap:

1. **Boolean-mask/sign-based rolling sums** (`volume_up_down_ratio_21d`):
   `(daily_ret > 0)` and `(daily_ret < 0)` are both `False` when `daily_ret`
   is NaN, so a missing day silently contributes `0` volume to *both* the
   up-volume and down-volume rolling sums instead of being excluded — the
   rolling `.sum()` itself never sees a NaN, so `min_periods` cannot catch
   this. Fixed by gating the final ratio with `require_full_window`.
2. **Cumulative-sum-based deltas** (`obv_momentum_21d`, `obv_momentum_63d`,
   `price_volume_trend_21d`): `np.sign(NaN)` is correctly `NaN` (so the sign
   itself is not fabricated), but `.cumsum()` treats NaN as a `0`
   contribution by default (`skipna=True`), so the cumulative series stays
   numeric across a gap instead of flagging it. The `LOOKBACK`-day delta
   (`obv - obv.shift(LOOKBACK)`, `pvt - pvt.shift(LOOKBACK)`) is therefore
   gated with `require_full_window(..., daily_ret, LOOKBACK)` so a gap
   anywhere in the trailing `LOOKBACK` window of `daily_ret` suppresses that
   day's indicator value.

One documented, intentional exception to the full-window default:

- **`vol_trend_slope_63d`** already implements its own robust internal
  minimum (`_vol_slope` requires `mask.sum() >= 20` valid points out of the
  63-length window passed to `np.polyfit`, tolerating up to 43 missing/NaN
  points before returning NaN) for the *outer* regression window. This is a
  deliberate, already-tested robustness choice for an OLS trend fit over a
  noisy derived (volatility) series, not an oversight — a trend slope
  legitimately tolerates non-contiguous points in a way a raw return
  average/variance should not. The *inner* `vol_21` computation still uses
  the full-window default (`rolling(21, min_periods=21)`). Kept as documented
  per §3.2 step 4 ("Record any intentional lower threshold in the inventory
  and unit test it"); see `test_vol_trend_slope_63d_gap_tolerance` in
  `signals/tests/indicators/volatility/test_volatility_indicators.py`.
- **Volume-only normalization windows** (`_NORM_WINDOW = 63` in the
  `volume/price_volume/*` and `volume/volume_trend/*` modules, used for
  `mean_vol = vol_wide.rolling(63, min_periods=44).mean()`) are left at their
  existing `min_periods`. These windows normalize by *volume*, not by a count
  of valid *returns* — §3.1's "N valid returns" default is a return-series
  policy; a volume-only rolling mean is out of scope for this bug (no
  price-derived return NaN can hide inside it, since volume never passes
  through `pct_change`). `force_index_13d`'s EMA (`ewm(span=13)`) is likewise
  left with its default EWM decay behavior: EWM is not a fixed N-lookback
  window (it has no closed calendar boundary to test for "spanning a gap"
  against), and pandas' `ignore_na=False` default (time-decay, not
  count-decay) is the standard convention for this estimator family — adding
  an artificial fixed-window gate would fight the estimator's actual
  semantics rather than fix a defect. Both are noted here rather than
  silently left unexamined, per the task's requirement to record and justify
  every non-migrated rolling policy choice.

## Call-site table

Columns: window(s) = the `.rolling`/lookback window(s) fed by the daily
return; `min_periods` = **before → after**; helper = whether the migrated
call uses `_price_utils.daily_return` (signals/indicators only) or a direct
`pct_change(fill_method=None)`.

### Phase 2 — momentum + volume batch (8 files)

| Module | Window(s) | min_periods before → after | Output | Helper | Notes |
|---|---|---|---|---|---|
| `signals/indicators/momentum/trend_quality/trend_consistency_21d.py` | 21 | 15 → 21 | `trend_consistency_21d_score` | `daily_return` | `.rolling(21,21).apply(...)` now only fires on gap-free windows. |
| `signals/indicators/momentum/trend_quality/trend_consistency_63d.py` | 63 | 44 → 63 | `trend_consistency_63d_score` | `daily_return` | Same pattern, 63d. |
| `signals/indicators/volume/volume_trend/volume_up_down_ratio_21d.py` | 21 | 15 → 21 (sum windows); ratio also gated | `volume_up_down_ratio_21d_score` | `daily_return` | Added `require_full_window` on the final ratio (mask-based sum defect, see above). |
| `signals/indicators/volume/price_volume/volume_weighted_momentum_21d.py` | 21 (return window); 63 (`_NORM_WINDOW`, volume-only, unchanged) | 15 → 21 | `volume_weighted_momentum_21d_score` | `daily_return` | `daily_ret * rel_vol` already propagates NaN; raising `min_periods` alone is sufficient. |
| `signals/indicators/volume/price_volume/price_volume_trend_21d.py` | 21 (`_LOOKBACK` delta); 63 (`_NORM_WINDOW` on `daily_ret.abs()*vol`) | `_NORM_WINDOW` 44 → 63; `_LOOKBACK` delta gated | `price_volume_trend_21d_score` | `daily_return` | `mean_flow` built from `daily_ret.abs()` (NaN propagates, so `min_periods=63` suffices there); `pvt_mom` (cumsum-based) additionally gated with `require_full_window(..., daily_ret, _LOOKBACK)`. |
| `signals/indicators/volume/price_volume/obv_momentum_63d.py` | 63 (`_LOOKBACK` delta); 63 (`_NORM_WINDOW`, volume-only, unchanged) | `_LOOKBACK` delta gated via `require_full_window` | `obv_momentum_63d_score` | `daily_return` | cumsum-skipna defect (see above). |
| `signals/indicators/volume/price_volume/obv_momentum_21d.py` | 21 (`_LOOKBACK` delta); 63 (`_NORM_WINDOW`, unchanged) | `_LOOKBACK` delta gated via `require_full_window` | `obv_momentum_21d_score` | `daily_return` | Same pattern, 21d. |
| `signals/indicators/volume/price_volume/force_index_13d.py` | 13 (EWM span, unchanged); 63 (`_NORM_WINDOW`, unchanged) | n/a | `force_index_13d_score` | `daily_return` | Only the `pct_change` call migrated; EWM tolerance left as documented exception above. |

### Phase 3 — volatility batch (20 files)

| Module | Window(s) | min_periods before → after | Output | Helper |
|---|---|---|---|---|
| `signals/indicators/volatility/systematic/idiosyncratic_vol_63d.py` | 63 (beta + residual std) | 44 → 63 | `idiosyncratic_vol_63d_score` | `daily_return` |
| `signals/indicators/volatility/systematic/beta_stability_63d.py` | 21 (inner beta); 63 (outer std of beta) | 15/44 → 21/63 | `beta_stability_63d_score` | `daily_return` |
| `signals/indicators/volatility/systematic/beta_63d.py` | 63 | 44 → 63 | `beta_63d_score` | `daily_return` |
| `signals/indicators/volatility/systematic/beta_252d.py` | 252 | 126 → 252 | `beta_252d_score` | `daily_return` |
| `signals/indicators/volatility/regime/vol_trend_slope_63d.py` | 21 (inner vol); 63 (outer slope, **documented exception**) | inner 15→21; outer 44 unchanged (internal `mask.sum()>=20` gate) | `vol_trend_slope_63d_score` | `daily_return` |
| `signals/indicators/volatility/regime/vol_ratio_21d_63d.py` | 21, 63 | 15/44 → 21/63 | `vol_ratio_21d_63d_score` | `daily_return` |
| `signals/indicators/volatility/regime/vol_ratio_21d_252d.py` | 21, 252 | 15/126 → 21/252 | `vol_ratio_21d_252d_score` | `daily_return` |
| `signals/indicators/volatility/regime/vol_percentile_252d.py` | 21 (inner vol); 252 (outer percentile rank) | 15/126 → 21/252 | `vol_percentile_252d_score` | `daily_return` |
| `signals/indicators/volatility/realized/vol_of_vol_21d.py` | 21 (inner vol); 63 (outer std) | 15/44 → 21/63 | `vol_of_vol_21d_score` | `daily_return` |
| `signals/indicators/volatility/realized/realized_vol_63d.py` | 63 | 44 → 63 | `realized_vol_63d_score` | `daily_return` |
| `signals/indicators/volatility/realized/realized_vol_252d.py` | 252 | 126 → 252 | `realized_vol_252d_score` | `daily_return` |
| `signals/indicators/volatility/realized/realized_vol_21d.py` | 21 | 15 → 21 | `realized_vol_21d_score` | `daily_return` |
| `signals/indicators/volatility/realized/realized_vol_10d.py` | 10 | 7 → 10 | `realized_vol_10d_score` | `daily_return` |
| `signals/indicators/volatility/downside/upside_deviation_63d.py` | 63 | 44 → 63 | `upside_deviation_63d_score` | `daily_return` |
| `signals/indicators/volatility/downside/up_down_vol_ratio_63d.py` | 63 (upside), 63 (downside) | 44/44 → 63/63 | `up_down_vol_ratio_63d_score` | `daily_return` |
| `signals/indicators/volatility/downside/downside_deviation_63d.py` | 63 | 44 → 63 | `downside_deviation_63d_score` | `daily_return` |
| `signals/indicators/volatility/adjusted_return/vol_adjusted_mom_12m.py` | 252 (vol only; `mom` uses `price_return`, not `pct_change`) | 126 → 252 | `vol_adjusted_mom_12m_score` | `daily_return` |
| `signals/indicators/volatility/adjusted_return/sortino_ratio_63d.py` | 63 (mean), 63 (downside) | 44/44 → 63/63 | `sortino_ratio_63d_score` | `daily_return` |
| `signals/indicators/volatility/adjusted_return/sharpe_ratio_252d.py` | 252 (mean), 252 (vol) | 126/126 → 252/252 | `sharpe_ratio_252d_score` | `daily_return` |
| `signals/indicators/volatility/adjusted_return/sharpe_ratio_63d.py` | 63 (mean), 63 (vol) | 44/44 → 63/63 | `sharpe_ratio_63d_score` | `daily_return` |

`signals/research/` — grepped and confirmed **zero** `pct_change()` call
sites (IC computation in `signals/research/ic.py` uses raw close-ratio
division per BUG-009, not `pct_change`; out of scope for BUG-010 either way).
No files to migrate in this directory.

### Phase 4 — non-signals batch (5 files) + `min_periods` policy

| Module | Series | Change | Notes |
|---|---|---|---|
| `backtesting/engine/data_handler.py::get_benchmark_returns_series` | Benchmark (SPY) close series | `pct_change()` → `pct_change(fill_method=None)` | Explicitly called out in §3.2 step 1 ("This explicitly includes benchmark returns in `DataHandler`"). A missing benchmark session now yields NaN (dropped by the existing `.dropna()`) instead of a forward-filled zero return silently entering backtest metrics/beta calculations. |
| `backtesting/engine/event_loop.py` (`_run_backtest`/equivalent) | Portfolio NAV series | `nav_series.pct_change().dropna()` → `nav_series.pct_change(fill_method=None).dropna()` | NAV is recorded once per simulated trading date by the event loop itself, so a gap is not expected in normal operation; fixed for consistency and defense-in-depth (a NAV recording bug elsewhere must not silently manufacture a zero return here). |
| `portfolio/risk_model/covariance.py::returns_from_prices` | Adjusted close price window | `window.pct_change()` → `window.pct_change(fill_method=None)` | Downstream `build_covariance` already drops sparse columns (>20% NaN) and NaN rows before fitting, so this call only needed the `fill_method` fix — the "valid observation" gating this bug asks for already exists here. |
| `reporting/dashboards/queries.py::daily_returns_for_tickers` | Multi-ticker close pivot | `pivot.pct_change().dropna(how="all")` → `pivot.pct_change(fill_method=None).dropna(how="all")` | Previously a per-ticker gap was forward-filled into a real (wrong) zero return that survived `dropna(how="all")` because other tickers' columns were non-NaN that day. Now that ticker's cell is correctly NaN for the gap day and only the gapped ticker's value is missing, not the whole row. |
| `reporting/dashboards/pages/5_Performance.py` | Strategy NAV history | `nav_filtered["nav_usd"].pct_change().dropna()` → `...pct_change(fill_method=None).dropna()` | Same NAV-gap rationale as `event_loop.py`. |

## Intentional behavior changes (recap)

1. **Warm-up period shrinks to nothing, `min_periods` == `window`** for every
   volatility/momentum/volume indicator's return-derived rolling statistic
   listed above. A score that previously appeared ~30% into its warm-up
   window (e.g. day 15 of a 21-day window) now first appears only once the
   full window is available (day 21) **and** stays NaN for any subsequent
   date whose trailing window contains a gap. This does not change any score
   value computed from a complete, gap-free window — only whether a
   partial/gapped window produces a (previously silently-biased) value or
   correctly suppresses it. Existing indicator tests use synthetic
   `pd.bdate_range` fixtures with no missing tickers/dates, so none of them
   exercise the warm-up boundary at a point that flips a currently-asserted
   value; no test assertions needed to change for this reason alone.
2. **`volume_up_down_ratio_21d`, `obv_momentum_21d`, `obv_momentum_63d`,
   `price_volume_trend_21d`**: a gap in the trailing lookback window now
   suppresses the indicator value entirely (previously it silently computed
   from whatever subset of days happened to have non-missing returns, via
   the mask-multiply / cumsum-skipna mechanisms described above).
3. **Benchmark/NAV/portfolio return series** (`data_handler.py`,
   `event_loop.py`, `covariance.py`, `queries.py`, `5_Performance.py`): a gap
   day now yields NaN and is dropped rather than silently becoming a
   forward-filled, non-NaN return that could distort a Sharpe ratio, beta, or
   covariance estimate downstream.

None of these changes alter the *sign* or *scale* of any indicator on
complete, gap-free data — every migrated formula is otherwise byte-identical
to before. Per-indicator sign/scale regression coverage: existing
`test_*_high_vol_scores_higher` / `test_*_high_beta_scores_higher` /
`test_*_better_risk_adj_return_scores_higher` behavioral tests in
`signals/tests/indicators/volatility/test_volatility_indicators.py` continue
to pass unchanged and serve as the §3.3 "retains intended sign/scale on
complete data" acceptance coverage; new gap tests (added in Phase 2-4, see
below) cover the "gap → NaN not zero" and "insufficient valid observations
suppress the value" acceptance criteria per indicator family.

## Cross-sectional scoring / minimum-eligible-count

`cross_sectional_zscore` (in `_price_utils.py`) z-scores whatever tickers are
present in a given date's row; `to_long` then drops NaN score cells. This
already implements §3.1's "Cross-sectional scoring may omit under-observed
ticker/date values" — an indicator's per-ticker NaN (from an incomplete
window) is excluded from that date's cross-section rather than imputed, and
never reaches the z-score with a fabricated value. No strategy-level minimum
eligible-count enforcement exists yet in the indicator layer itself (that
lives in the scorer/portfolio-construction layer, out of this bug's file
scope); this inventory does not add one, since BUG-010's stated boundary is
the price-return calculation, not cross-sectional eligibility policy.

## Guard test

`tests/test_pct_change_guard.py` scans `signals/`, `backtesting/`,
`portfolio/`, `reporting/` (excluding test directories) for any
`.pct_change(` call lacking a same-line `fill_method=None`, and fails with a
`file:line` list plus the offending source line for each violation found. A
genuinely justified new exception must be added to the test's
`_DOCUMENTED_EXCEPTIONS` dict with a file/line/reason, not silenced by
loosening the regex. As of the end of Phase 4 (full migration), this test
passes with zero violations across the 33 call sites inventoried above.
