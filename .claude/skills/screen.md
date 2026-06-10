# screen

Queries the `alpha_scores` table to screen tickers by rank or score
threshold. Use this to see what the signal is currently saying without
recomputing scores from scratch.

## Usage

```
/screen [options]
```

**options** — any of:
- `date=YYYY-MM-DD` — score date to query (default: latest in alpha_scores)
- `strategy=v1_base_momentum` — strategy_id (default: v1_base_momentum)
- `top=20` — return the N highest-ranked tickers (default: 20)
- `bottom=10` — also return the N lowest-ranked tickers (default: 0)
- `min_score=0.5` — filter to tickers with alpha_score ≥ this value
- `tickers=AAPL,MSFT,...` — look up specific tickers regardless of rank

## What this skill does

1. Resolve `date`: if not provided, `SELECT MAX(score_date) FROM alpha_scores WHERE strategy_id = :strategy`.
2. Query `alpha_scores` for that date and strategy, ordered by rank ASC.
3. Apply `top`, `bottom`, `min_score`, or `tickers` filters as specified.
4. Join to `factor_scores` to show per-factor z-scores alongside the composite.

## Output format

```
Screen — 2026-06-10   Strategy: v1_base_momentum   Universe: 503 tickers

Rank  Ticker   Alpha Score   Momentum Z   Lowvol Z   Value Z   Quality Z
   1  NVDA       1.842         2.31         1.35        —         —
   2  META       1.601         1.98         1.22        —         —
  ...

(— means factor not available for this date/strategy)
```

If `date` has no scores yet (backfill not run, or DAG hasn't fired):
```
No alpha_scores found for date=2026-06-10 strategy=v1_base_momentum.
Latest available date: 2026-06-09. Re-run with date=2026-06-09 or trigger
the daily_signal_pipeline DAG.
```

## Safety

- Read-only. Queries only `alpha_scores` and `factor_scores`.
- Does not connect to the broker.
- Never displays order recommendations — ranks are research output only.
