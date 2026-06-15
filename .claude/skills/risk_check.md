# Skill: `risk_check`

## Purpose

Compute a real-time risk snapshot for the current live portfolio and display
all metric values and any threshold breaches.  Read-only — no orders, no
broker mutations.

Safe to invoke autonomously.

---

## When to invoke

- Daily morning risk review before trading opens
- After a large market move (>2% SPY daily)
- Before invoking `execute_trade` (pre-trade risk gate)
- Any time the operator wants current risk metrics

---

## Required inputs

| Input | Type | Notes |
|-------|------|-------|
| `positions` | dict | Current positions {ticker: shares} from broker or DB |
| `prices` | dict | Current prices {ticker: last_close} |
| `nav` | float | Current portfolio NAV |

### Optional inputs

| Input | Type | Default | Notes |
|-------|------|---------|-------|
| `lookback_days` | int | 252 | Days of return history for historical VaR |
| `benchmark_ticker` | str | SPY | Benchmark for beta calculation |
| `sector_map` | dict | from config | {ticker: GICS_sector} for sector concentration |

---

## Outputs

Displays a formatted risk report including:

```
=== RQIS Risk Snapshot — 2024-06-15 ===

NAV:              $1,234,567
Peak NAV:         $1,300,000
Drawdown:          -5.03%          ⚠ WARNING (hard: -10%)

VaR (1d 99%):      1.82%          ✓ OK     (hard: 2.50%)
CVaR (1d 99%):     2.31%          ✓ OK
Portfolio Beta:    1.24            ✓ OK     (hard: 1.50)
Max Concentration: 4.8% (AAPL)    ✓ OK     (hard: 5.0%)
Max Sector Wt:    22.1% (Tech)    ✓ OK     (hard: 25.0%)

Breaches: 1 WARNING (drawdown)
Circuit Breaker:  CLOSED ✓
```

Returns a `RiskSnapshot` object for programmatic use.

---

## Programmatic usage

```python
import yaml
from datetime import date
import pandas as pd

from portfolio.risk_model.covariance import build_covariance, returns_from_prices
from risk.realtime.monitor import RiskMonitor
from risk.alerts.alert_manager import AlertManager
from risk.circuit_breaker import CircuitBreaker

# Load thresholds from settings
with open("config/settings.yaml") as f:
    import yaml
    settings = yaml.safe_load(f)

monitor = RiskMonitor.from_config(settings["risk"])
alert_manager = AlertManager()
circuit_breaker = CircuitBreaker()

# Build portfolio weights from positions + prices
total_nav = sum(positions.get(t, 0) * prices.get(t, 0) for t in positions)
weights = pd.Series({
    t: (positions[t] * prices[t]) / total_nav
    for t in positions if prices.get(t, 0) > 0
})

# as_of must be provided by the caller — never use date.today() directly.
# (Using wall-clock date violates the simulation clock convention and produces
# incorrect results when run after market hours.)
as_of: date  # caller must supply this — e.g. date(2024, 6, 15)

# Historical returns (from DB)
returns = returns_from_prices(prices_df, as_of=as_of)
cov = build_covariance(returns)

# Compute risk snapshot
snap = monitor.snapshot(
    as_of=as_of,
    nav=total_nav,
    weights=weights,
    portfolio_returns=portfolio_return_series,
    asset_returns=asset_return_df,
    benchmark_returns=benchmark_return_series,
    covariance=cov,
    sector_map=sector_map,
)

# Fire alerts (informational only — does not mutate circuit breaker state)
alerts = alert_manager.fire_from_snapshot(snap)

# Read circuit breaker state — READ-ONLY. Do NOT call circuit_breaker.evaluate()
# here; that is a state mutation that can trip the breaker. risk_check is
# diagnostic only. The monitoring pipeline calls evaluate() separately.
cb_state = circuit_breaker.state.value  # "CLOSED" or "OPEN"

# Report
print(f"Drawdown: {snap.drawdown:.2%}")
print(f"VaR 1d 99%: {snap.var_1d_99:.2%}")
print(f"Beta: {snap.portfolio_beta:.2f}")
print(f"Max conc: {snap.max_concentration:.2%}")
print(f"Circuit breaker: {cb_state}")
if snap.breaches:
    print(f"\n⚠ {len(snap.breaches)} BREACH(ES):")
    for b in snap.breaches:
        print(f"  {b['severity'].upper()}: {b['metric']} = {b['value']:.4f} (threshold {b['threshold']:.4f})")
```

---

## Safety notes

- Read-only — no broker calls, no state mutations.
- If the circuit breaker is OPEN, report that prominently.  The operator must
  reset it manually (C4) before trading can resume.
- If any HARD breach is detected, do **not** proceed to `execute_trade`.
  Tell the operator and wait for their instruction.
