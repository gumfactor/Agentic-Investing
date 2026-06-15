# Skill: `portfolio_construct`

## Purpose

Construct a target portfolio from the latest alpha scores using the configured
optimizer (MVO, risk-parity, or equal-weight) and constraint handler.  Produces
**STAGED orders only** — no broker submission.

This skill is safe to invoke autonomously — it does **not** submit orders to
any broker.  Output is a list of STAGED orders that must be reviewed and
approved by the `execute_trade` skill.

---

## When to invoke

- Scheduled rebalance day (calendar or drift trigger)
- Operator requests a portfolio construction run
- After a new round of alpha scores has been computed

---

## Required inputs

| Input | Type | Notes |
|-------|------|-------|
| `strategy_config_path` | str | Path to strategy YAML, e.g. `config/strategy/v2_mvo_momentum.yaml` |
| `as_of_date` | str | ISO date; all signals must have `score_date < as_of_date` (PIT) |

### Optional inputs

| Input | Type | Default | Notes |
|-------|------|---------|-------|
| `current_positions` | dict | {} | Current broker positions {ticker: shares} |
| `current_nav` | float | from broker | Portfolio NAV for weight calculations |
| `prices` | dict | from DB | {ticker: last_close_price} |

---

## Outputs

- **STAGED order list** displayed for operator review (one row per order)
- Risk snapshot at target weights (pre-trade VaR, max concentration, beta)
- Cost estimate (total estimated transaction costs)

The STAGED orders are stored in the `OrderManager` and are ready for the
`execute_trade` skill to run compliance and submit.

---

## Programmatic usage

```python
import yaml
from datetime import date

from portfolio.optimization import MVOOptimizer, RiskParityOptimizer
from portfolio.risk_model.constraints import PortfolioConstraints
from portfolio.risk_model.covariance import build_covariance, returns_from_prices
from execution.oms.order import Order, OrderSide
from execution.oms.order_manager import OrderManager

# 1. Load config
with open("config/strategy/v2_mvo_momentum.yaml") as f:
    cfg = yaml.safe_load(f)

# 2. Pull latest signals (PIT-safe)
# alpha_scores: DataFrame with columns [score_date, ticker, alpha_score]
# Keep only top N by alpha_score
top_n = cfg["portfolio"]["n_long"]
today = date.fromisoformat(as_of_date)
visible = alpha_scores[alpha_scores["score_date"] < today]
latest = visible.sort_values("score_date").groupby("ticker").last()
candidates = latest.nlargest(top_n, "alpha_score")

# 3. Build covariance matrix
returns = returns_from_prices(prices_df, as_of=today, lookback_days=252)
cov = build_covariance(returns, method="ledoit_wolf")

# 4. Expected returns from alpha scores (scaled to annual)
mu = candidates["alpha_score"]

# 5. Run optimizer
method = cfg["portfolio"]["method"]
constraints = PortfolioConstraints.from_config(cfg.get("constraints", {}))

if method == "mvo":
    optimizer = MVOOptimizer(mode=cfg["portfolio"]["optimizer_mode"])
elif method == "risk_parity":
    optimizer = RiskParityOptimizer()
else:
    # Equal weight fallback
    n = len(candidates)
    from portfolio.optimization import OptimizationResult
    import pandas as pd
    result = OptimizationResult(
        weights=pd.Series(1/n, index=candidates.index),
        objective_value=0.0,
        solver_status="equal_weight",
        diagnostics={},
    )

if method in ("mvo", "risk_parity"):
    result = optimizer.run(mu, cov, constraints)

# 6. Convert to orders
target_weights = result.weights
# ... compute weight deltas vs. current positions, create Order objects
orders = []
for ticker, target_w in target_weights.items():
    current_w = current_weights.get(ticker, 0.0)
    delta_w = target_w - current_w
    if abs(delta_w) < 0.001:
        continue
    notional = abs(delta_w) * current_nav
    price = prices.get(ticker, 0.0)
    qty = notional / price if price > 0 else 0
    side = OrderSide.BUY if delta_w > 0 else OrderSide.SELL
    orders.append(Order(ticker=ticker, side=side, quantity=round(qty), limit_price=price))

# 7. Stage orders in OMS
om = OrderManager()
om.stage_batch(orders)
print(f"Staged {len(orders)} orders. Run execute_trade to submit.")
```

---

## Safety notes

- **Never** call broker submission from this skill.  Output is STAGED orders only.
- PIT safety: signals must have `score_date < as_of_date` (enforced by DataHandler).
- All orders start in STAGED status and must pass compliance before PENDING.
- Weights are checked against constraint limits before order generation; soft
  violations are logged as warnings.
