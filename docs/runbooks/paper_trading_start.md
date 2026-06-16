# Paper Trading Start Runbook

Step-by-step procedure for beginning RQIS Phase 4 paper trading.  
Complete this entire checklist before week 1 begins.

**Phase gate:** 4 consecutive weeks of clean paper trading with zero critical bugs  
**Safety rule:** C8 — never switch to live capital without passing this gate.

---

## Prerequisites

Before starting, confirm all of the following:

- [ ] IBKR TWS or Gateway installed on your local machine
- [ ] IBKR paper trading account funded and activated (separate from live account)
- [ ] Docker Desktop running; `make up` (or `docker compose up -d`) starts the stack cleanly
- [ ] `python scripts/check_pipeline_health.py` reports OK on all tables
- [ ] Git branch `claude/elegant-newton-vtzrmh` merged to main (or you are on a branch with all Phase 4 code)
- [ ] 102 tests pass: `pytest -q` returns no failures

---

## Step 1 — Configure your `.env`

Copy the example if you haven't already:

```powershell
cp .env.example .env
```

Edit `.env` and set the IBKR block:

```dotenv
IBKR_HOST=127.0.0.1
IBKR_PORT=7497          # paper port — do NOT change to 7496
IBKR_CLIENT_ID=1
PAPER_TRADING=true      # do NOT change this — C8/C9
```

Double-check with:

```powershell
grep -E "IBKR|PAPER" .env
```

Expected output:
```
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=1
PAPER_TRADING=true
```

If `IBKR_PORT=7496` or `PAPER_TRADING=false` appear — **stop and fix before continuing**.

---

## Step 2 — Start IBKR TWS (paper session)

1. Launch **Trader Workstation** (TWS) or **IB Gateway** on your local machine.
2. Log in with your **paper trading credentials** (separate username from live account).
3. In TWS: go to **Edit → Global Configuration → API → Settings**.
   - Enable **"Enable ActiveX and Socket Clients"**
   - Set **Socket port** to `7497`
   - Check **"Allow connections from localhost only"**
   - Uncheck **"Read-Only API"** (RQIS needs to submit orders)
4. Click **OK / Apply**.
5. Confirm TWS shows: `API Connected` in the status bar.

> **IB Gateway alternative (recommended for unattended runs):**
> Use IB Gateway instead of TWS — it uses less RAM and does not time out the paper session.
> Set paper port to 7497 in IB Gateway settings.

---

## Step 3 — Verify broker connectivity

Run the connectivity check (no orders are placed — read-only):

```powershell
python -c "
from execution.brokers.ibkr import IBKRBroker
import os, json

b = IBKRBroker(
    host=os.environ.get('IBKR_HOST', '127.0.0.1'),
    port=int(os.environ.get('IBKR_PORT', 7497)),
    client_id=int(os.environ.get('IBKR_CLIENT_ID', 1)),
)
b.connect()
print('Account value:', b.get_account_value())
print('Positions:', json.dumps(b.get_positions(), indent=2))
print('is_paper:', b.is_paper)
b.disconnect()
print('Connection OK')
"
```

Expected output contains:
```
Account value: <some positive number>
is_paper: True
Connection OK
```

If you see `is_paper: False` — **stop immediately**. You are connected to a live account.  
Disconnect, verify `IBKR_PORT=7497`, and restart.

---

## Step 4 — Run the circuit breaker fire drill

This drill requires no broker connection and runs in under 5 seconds.  
It must pass with all checks green before week 1.

```powershell
python scripts/cb_fire_drill.py
```

Expected final line:
```
✓  ALL 43 CHECKS PASSED
```

If any check fails: investigate before proceeding.  
The drill output identifies exactly which component failed.

---

## Step 5 — Run the daily pipeline end-to-end (dry run)

Trigger the full daily pipeline manually and confirm it completes without errors:

1. Open the Airflow UI: `http://localhost:8080`
2. Go to **DAGs → daily_data_pipeline → Trigger DAG ▶**
3. Watch the Graph view until all tasks show green.
4. Verify with:

```powershell
python scripts/check_pipeline_health.py
```

Expected:
```
daily_prices  rows=...   tickers=...   latest_date=<today or most recent market day>   OK
quality_flags rows=...                                                                  OK
Pipeline health: OK
```

Confirm that `latest_date` matches the most recent trading day (yfinance data lags by one day after market close).

---

## Step 6 — Run a score backfill dry run

Verify signal scores are current:

```powershell
python scripts/backfill_momentum_scores.py --dry-run
```

If scores are stale (>1 trading day behind), run without `--dry-run` to backfill, then re-pin the snapshot:

```powershell
python scripts/pin_snapshot.py --strategy-id v1 --benchmark SPY
```

Record the manifest path in your session notes.

---

## Step 7 — Stage and review the first rebalance

Run portfolio construction to generate staged orders:

```powershell
python -m rqis.skills.portfolio_construct --strategy-id v1 --as-of <today>
```

Review the staged order list carefully:

- All sides correct (BUY/SELL match your intended changes)
- All tickers are in the S&P 500 universe
- No order exceeds 20% single-name weight
- Sector weights remain below 50% post-trade
- Total turnover is within expectations (~2–5% on first run)

> **C1 reminder:** Staged orders are NOT submitted to IBKR at this point.  
> Submission requires a separate explicit `"YES"` from you via the `execute_trade` skill.

---

## Step 8 — Run compliance pre-check

```powershell
python -m rqis.skills.risk_check --as-of <today>
```

Confirm:
- `circuit_breaker_state: CLOSED`
- No hard breaches
- VaR within limits
- All sector weights below hard threshold

---

## Step 9 — Submit paper orders (C1 confirmation)

Run the `execute_trade` skill:

```powershell
python -m rqis.skills.execute_trade --strategy-id v1
```

The skill will display the full order list and prompt:

```
=== PENDING ORDERS ===
[table of orders with ticker, side, qty, limit_price, estimated notional]

Type YES to submit all orders to IBKR paper account (port 7497), or anything else to abort:
```

**Read every row.** Type `YES` only after confirming the list is correct.

---

## Week 1–4 Daily Monitoring Checklist

Run each trading day after market close (or before next open):

```powershell
# 1. Pipeline health
python scripts/check_pipeline_health.py

# 2. Risk snapshot (circuit breaker status, VaR, drawdown)
python -m rqis.skills.risk_check --as-of <today>

# 3. Fill reconciliation (check all SUBMITTED orders resolved to FILLED or REJECTED)
python -m rqis.skills.monitor --as-of <today>
```

Log any anomalies to `Worklog.md` with a `[SAFETY]` tag.

---

## Weekly Review Checklist

At end of each week:

| Check | Expected |
|-------|----------|
| Circuit breaker state | CLOSED |
| All submitted orders FILLED or CANCELLED | No stuck SUBMITTED/PENDING orders |
| Drawdown from peak | < −5% warning, < −10% hard limit |
| Portfolio beta | < 1.3 warning, < 1.5 hard limit |
| Max single-name weight | < 4% warning, < 5% hard limit |
| Sharpe (weekly proxy) | > 0 preferred |
| Any REJECTED compliance orders | Investigate reason; log in Worklog |
| Any stuck PENDING orders | Investigate (Phase 5 deferred item) |
| No critical bugs in logs | Zero tolerance |

---

## If the Circuit Breaker Fires

1. **Do not reset automatically.** Review the `RiskMonitor.snapshot()` output to understand the breach.
2. Identify the metric that triggered: drawdown, VaR, beta, or concentration.
3. If it is a data error (e.g. stale price, bad corporate action): document in `Worklog.md` with `[SAFETY]` tag, then:

```python
from risk.circuit_breaker import CircuitBreaker
cb = CircuitBreaker()
# ... (load from your runtime state)
cb.reset(operator="your@email.com", reason_code="FALSE_POSITIVE_BAD_PRICE_DATA")
```

4. If it is a genuine loss breach: **do not reset.** Review the portfolio, assess whether to exit positions, and wait for the situation to resolve before resetting.

> **C4:** Only a human with a documented reason code may reset the circuit breaker.  
> Automatic resets are not possible by design.

---

## Phase 5 Entry Criteria

All four of the following must be true before moving to Phase 5 (Reporting + Live Trading):

| Criterion | Verified by |
|-----------|-------------|
| 4 consecutive weeks of paper trading | Weekly review logs in `Worklog.md` |
| Zero critical bugs (no REJECTED orders from bugs, no data corruption) | Log review |
| Circuit breaker fire drill passed | `scripts/cb_fire_drill.py` exit code 0 |
| Operator `"YES"` sign-off | Entry in `Worklog.md` with `[DECISION]` tag |

> **C8:** Switching to live capital without passing all four criteria is prohibited.
