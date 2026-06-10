# signal_research

Runs IC analysis on a named factor against live price data. Use this to
validate whether a factor predicts forward returns before including it in
the composite.

## Usage

```
/signal_research [factor] [options]
```

**factor** — one of: `momentum`, `lowvol`, `value`, `quality`  
**options** — any of:
- `horizons=1,5,21,63` — forward return horizons in trading days (default: all four)
- `since=YYYY-MM-DD` — restrict IC series to this start date
- `strategy=v1_base_momentum` — strategy_id label for MLflow logging (default: v1_base_momentum)

## What this skill does

1. Load daily prices from `daily_prices` for the trailing 3 years (or since `since`).
2. Compute factor scores using the appropriate function:
   - `momentum` → `signals.factors.momentum.compute_momentum_scores`
   - `lowvol` → `signals.factors.low_vol.compute_lowvol_scores`
   - `value` → `signals.factors.value.compute_value_scores` (requires fundamentals)
   - `quality` → `signals.factors.quality.compute_quality_scores` (requires fundamentals)
3. Run `signals.research.ic.compute_ic_series` to get per-date IC at each horizon.
4. Run `signals.research.ic.summarize_ic` to compute mean IC, IC-IR, t-stat, and p-value.
5. Run `signals.research.ic.rolling_ic_summary` (trailing_dates=252) to show walk-forward stability.
6. Run `signals.research.ic.compute_factor_turnover` (rebalance_days=21) for monthly rank autocorrelation.
7. Run `signals.research.universe.audit_universe_survivorship` and attach the bias warning to all output.

## Output format

Print a summary table:

```
Factor: momentum   Strategy: v1_base_momentum   Universe: 503 tickers
Survivorship bias: MODERATE — 18% late entrants. Results provisional.

IC Summary (Pearson / Spearman)
Horizon   Mean IC   Mean Rank-IC   IC-IR   t-stat   p-value   N dates
1d        ...
5d        ...
21d       ...
63d       ...

Turnover (21-day rebalance)
  Mean rank autocorrelation: X.XX  (higher = lower turnover)

Rolling IC (trailing 252 dates) — last 6 snapshots
Date        H21 IC   H63 IC   Hit rate
...
```

Flag horizons where p-value > 0.10 as ⚠ not significant.
Flag mean IC < 0 as ✗ negative.

## Safety

- Read-only. Does not write to any table.
- Does not connect to the broker.
- If fundamentals are absent (value/quality), prints a clear message and stops rather than producing empty results silently.
