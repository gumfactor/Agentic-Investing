# score

Computes composite alpha scores for all available factors as of a given date
and displays the ranked universe. Use this to see today's signal or to
inspect scores for a specific historical date.

## Usage

```
/score [options]
```

**options** — any of:
- `date=YYYY-MM-DD` — score date (default: latest date in daily_prices)
- `strategy=v1_base_momentum` — strategy_id (default: v1_base_momentum)
- `top=20` — show this many top-ranked tickers (default: 20)
- `bottom=20` — also show this many bottom-ranked tickers (default: 10)
- `weights=momentum:3,lowvol:1` — override equal-weight blending

## What this skill does

1. Resolve `date`: if not provided, query `SELECT MAX(date) FROM daily_prices`.
2. Load prices: trailing 400 days ending on `date` from `daily_prices`.
3. Compute each available factor:
   - Always: `momentum`, `lowvol`
   - If `financial_statements` has rows: `value`, `quality`
   - Log which factors were available.
4. Call `signals.scoring.scorer.combine_factor_scores` with the available factors,
   `score_date=date`, `strategy_id=strategy`, and `weights` if provided.
5. Display results.

## Output format

```
Alpha scores — 2026-06-10   Strategy: v1_base_momentum
Factors used: momentum, lowvol   (value, quality: no fundamentals)
Universe: 503 tickers

Rank  Ticker   Alpha Score   Momentum Z   Lowvol Z
   1  NVDA       1.842         2.31         1.35
   2  META       1.601         1.98         1.22
  ...
  20  ...

--- bottom 10 ---
 494  ...
```

If `weights` were overridden, show the normalised weights used.

## Safety

- Read-only. Does not write to any table and does not trigger any orders.
- Does not connect to the broker.
- If no prices are found for `date`, prints an error and stops.
