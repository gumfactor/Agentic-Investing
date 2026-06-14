# Skill: `backtest`

## Purpose

Run a complete event-driven backtest of a versioned strategy configuration.
Produces a `BacktestResult` (NAV series, daily returns, trade log, metrics)
and logs everything to MLflow.

This skill is safe to invoke autonomously — it does **not** submit broker orders.
All output is written to MLflow and optionally to local parquet files.

---

## When to invoke

- Operator requests a backtest of a strategy config (e.g. "run a backtest of v1_base_momentum")
- Validating a new factor or strategy variant
- Reproducing a previous run by run_id

---

## Required inputs

| Input | Type | Notes |
|-------|------|-------|
| `config_path` | str | Path to strategy YAML, e.g. `config/strategy/v1_base_momentum.yaml` |
| `data_version` | str | Dataset manifest path, e.g. `rqis-snapshots/manifests/2026-06-14/manifest.json` (required per C7 — refuse to run without it) |
| `experiment_name` | str | MLflow experiment, e.g. `base_momentum/momentum` |

### Optional inputs

| Input | Type | Default | Notes |
|-------|------|---------|-------|
| `start_date` | str | config value | Override backtest start (ISO format) |
| `end_date` | str | config value | Override backtest end (ISO format) |
| `fill_model` | str | `transaction_cost` | `perfect` for cost-free baseline comparison |
| `run_name` | str | auto | Human-readable MLflow run name |

---

## Outputs

- **MLflow run** logged at `experiment_name` containing:
  - All config params (flattened)
  - Performance metrics: Sharpe, CAGR, MaxDrawdown, IR, Turnover
  - `data_version` tag (C7)
  - `config_hash` tag (SHA-256 of full config for reproducibility)
  - Artifacts: `config.json`, `returns.csv`, `metrics.json`, `trades.csv`
- Returns the **MLflow run_id** to the operator

---

## Invocation example

```
Run a backtest of config/strategy/v1_base_momentum.yaml.
Data version: rqis-snapshots/manifests/2026-06-14/manifest.json
MLflow experiment: base_momentum/momentum
```

---

## Programmatic usage

```python
import yaml
from backtesting.engine.data_handler import DataHandler
from backtesting.engine.event_loop import BacktestEngine
from backtesting.engine.fill_simulator import FillSimulator
from backtesting.experiment_tracking.mlflow_logger import BacktestLogger

# 1. Load config
with open("config/strategy/v1_base_momentum.yaml") as f:
    config = yaml.safe_load(f)
config["data_version"] = "rqis-snapshots/manifests/2026-06-14/manifest.json"

# 2. Load data (prices and pre-computed alpha_scores from DB or parquet)
# prices: long DataFrame with ticker, date, close
# alpha_scores: long DataFrame with ticker, score_date, alpha_score
# benchmark: long DataFrame with date, close (e.g. SPY)
data_handler = DataHandler(prices=prices, alpha_scores=alpha_scores, benchmark=benchmark)

# 3. Run engine
engine = BacktestEngine()
fill_sim = FillSimulator(
    bid_ask_spread_bps=config["execution"]["bid_ask_spread_bps"],
    market_impact_coeff=config["execution"]["market_impact_coeff"],
    commission_per_share=config["execution"]["commission_per_share"],
    fill_model=config["execution"]["fill_model"],
)
result = engine.run(config, data_handler, fill_sim)

# 4. Log to MLflow (enforces C7)
bt_logger = BacktestLogger()
run_id = bt_logger.log_run(
    config=config,
    result=result,
    experiment_name="base_momentum/momentum",
)
print(f"Run ID: {run_id}")
print(f"Sharpe: {result.metrics['sharpe']:.3f}")
print(f"CAGR: {result.metrics['cagr']:.2%}")
print(f"MaxDD: {result.metrics['max_drawdown']:.2%}")
```

---

## Safety notes

- **C7**: Refuses to log without `data_version`. This is enforced in `BacktestLogger.log_run()`.
- **C6**: Never modify a strategy config YAML that has been used in a live session. Create a new versioned file (`v{N+1}_...yaml`) instead.
- No broker connection. No capital at risk.
- `perfect` fill mode removes all transaction costs — use only for signal validation, never to report live-deployment performance.

---

## Known limitations

- Currently supports **equal-weight** portfolio construction only. Mean-variance and risk-parity are planned for Phase 4.
- Survivorship bias: the S&P 500 universe uses current-day membership. Phase 3 uses the same universe for research continuity; a point-in-time constituent file should replace it before live deployment.
- ADV (average daily volume) is not currently piped into the fill simulator, so market impact uses the default 5% participation rate assumption.
- Intraday fills are not supported — all fills execute at the prior day's closing price.
