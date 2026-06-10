# Skill: `attribute`

## Purpose

Decompose portfolio returns into factor contributions and sector allocation/selection effects.
Two complementary attribution methods are available:

1. **Brinson-Hood-Beebower (BHB)**: Decomposes excess return into allocation, selection,
   and interaction effects for a grouping dimension (e.g. GICS sector).
2. **Factor decomposition**: OLS regression of portfolio excess returns on factor returns
   to quantify market, sector, and style exposures versus pure alpha.

This skill is safe to invoke autonomously — it is read-only and produces no trades.

---

## When to invoke

- After a backtest completes: `attribute` turns the return series into an explanation
- During live paper trading: identify whether excess returns come from factor tilts or stock selection
- Monthly reporting cycle: factor decomposition feeds directly into the investor tearsheet

---

## Required inputs

| Input | Type | Notes |
|-------|------|-------|
| `backtest_run_id` | str | MLflow run_id from the `backtest` skill (loads returns and trades) |
| `attribution_type` | str | `brinson` or `factor` or `both` |

### For Brinson attribution

| Input | Type | Notes |
|-------|------|-------|
| `portfolio_weights` | DataFrame | date, ticker, weight, sector (or group_col) |
| `benchmark_weights` | DataFrame | date, ticker, weight, sector (or group_col) |
| `security_returns` | DataFrame | date, ticker, return |
| `group_col` | str | Column to group by (default: `sector`) |

### For factor decomposition

| Input | Type | Notes |
|-------|------|-------|
| `portfolio_returns` | Series | Daily portfolio returns (index=date) |
| `factor_returns` | DataFrame | Daily factor returns (index=date, cols=factor names) |
| `risk_free_returns` | float or Series | Default: 0.0 |

---

## Outputs

### Brinson output

- `AttributionResult.summary`: per-sector cumulative allocation / selection / interaction
- `AttributionResult.total_excess_return`: total active return explained
- `AttributionResult.records`: per (date, sector) attribution records

### Factor decomposition output

- `FactorDecompositionResult.factor_betas`: estimated exposure per factor
- `FactorDecompositionResult.alpha`: annualised stock-specific return (intercept)
- `FactorDecompositionResult.r_squared`: fraction of return variance explained by factors
- `FactorDecompositionResult.t_stats` / `.p_values`: statistical significance
- `FactorDecompositionResult.residuals`: unexplained daily returns

---

## Invocation example

```
Run Brinson attribution on backtest run abc123.
Benchmark: SPY at GICS sector level.
Also run factor decomposition using Fama-French 3-factor returns.
```

---

## Programmatic usage

### Brinson attribution

```python
from backtesting.attribution.brinson import compute_brinson_attribution

result = compute_brinson_attribution(
    portfolio_weights=portfolio_weights_df,   # date, ticker, weight, sector
    benchmark_weights=benchmark_weights_df,   # date, ticker, weight, sector
    returns=security_returns_df,              # date, ticker, return
    group_col="sector",
)

print(result.summary)
print(f"Total allocation effect: {result.total_allocation:.4f}")
print(f"Total selection effect: {result.total_selection:.4f}")
print(f"Total excess return: {result.total_excess_return:.4f}")
```

### Factor decomposition

```python
from backtesting.attribution.factor_decomposition import (
    decompose_factor_returns,
    compute_factor_contributions,
)

decomp = decompose_factor_returns(
    portfolio_returns=portfolio_returns,      # pd.Series, index=date
    factor_returns=factor_returns_df,         # pd.DataFrame, index=date, cols=factors
)

print(f"Alpha (annualised): {decomp.alpha:.4f}")
print(f"R²: {decomp.r_squared:.3f}")
print(decomp.factor_betas)
print(decomp.t_stats)

# Daily factor contributions
contributions = compute_factor_contributions(decomp.factor_betas, factor_returns_df)
```

---

## Safety notes

- No broker connection. Read-only.
- Brinson attribution requires security-level returns and weights aligned on dates.
  Misaligned dates produce zeros not errors — verify date coverage before trusting the output.
- Factor decomposition uses OLS with heteroscedasticity-robust standard errors (HC3).
  With short time series (< 60 observations), betas and t-stats should be treated as rough estimates.
- Attribution results are informational only — they do not trigger rebalancing or order generation.

---

## Known limitations

- Brinson implementation uses arithmetic (not geometric) attribution. For multi-period
  attribution, apply the linking method (e.g. Modified Dietz) before calling this skill.
- Factor decomposition does not correct for autocorrelation in returns. For meaningful
  t-stats over holding periods > 1 day, use Newey-West SE (available in `signals/research/ic.py`).
- Sector mapping must be provided by the caller; the skill does not auto-assign sectors.
