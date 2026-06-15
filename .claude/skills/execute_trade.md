# Skill: `execute_trade`

## Purpose

Submit PENDING orders to the IBKR paper (or live) broker after compliance
checks pass and the operator provides explicit "YES" confirmation.

**THIS SKILL IS NOT SAFE TO INVOKE AUTONOMOUSLY.**
It requires literal `"YES"` from the operator before any order is submitted
(safety rule C1).  Never proceed to submission without this confirmation.

---

## When to invoke

- After `portfolio_construct` has staged orders
- After `risk_check` has passed (no HARD breaches, circuit breaker CLOSED)
- Operator explicitly requests trade execution

---

## Required inputs

| Input | Type | Notes |
|-------|------|-------|
| `order_ids` | list[str] | Order IDs to submit (from `portfolio_construct` output) |

### Optional inputs

| Input | Type | Default | Notes |
|-------|------|---------|-------|
| `dry_run` | bool | false | If true, display orders but do not submit |
| `timeout_seconds` | int | 300 | Time to wait for fills before reporting partial |

---

## Execution protocol (MANDATORY — never skip any step)

```
Step 1: Verify circuit breaker is CLOSED
        If OPEN → stop immediately, tell operator circuit breaker must be
        reset (C4) by a human with a reason code before trading.

Step 2: Run pre-trade compliance on all PENDING orders
        If any order REJECTED → remove it from submission list, show reason.
        If ALL orders are rejected → stop. Do NOT ask for YES on an empty batch.

Step 3: Display the full order table to the operator:
        ┌──────────┬────────┬──────┬──────────┬────────────┐
        │ order_id │ ticker │ side │ quantity │ limit_price│
        ├──────────┼────────┼──────┼──────────┼────────────┤
        │ a1b2c3d4 │ AAPL   │ BUY  │      150 │     182.50 │
        │ e5f6g7h8 │ MSFT   │ SELL │       80 │     415.20 │
        └──────────┴────────┴──────┴──────────┴────────────┘
        Total orders: 2 | Estimated cost: $312 | Net notional: $54,218

Step 4: Ask operator: "Type YES to submit these orders, or anything else to cancel."
        Wait for response.

        The confirmation must be the EXACT 3-character string `YES` with no
        surrounding text. Inputs like "yes please", "YES go ahead", or "yes"
        do not count. If in doubt, ask the operator to type only `YES`.

Step 5a: If response == "YES" (exact, case-sensitive, nothing else):
         → Call order_manager.submit_pending()
         → Log submission to audit trail
         → Poll for fills (up to timeout_seconds)
         → Report fill summary

Step 5b: If response is anything other than exact "YES":
         → Cancel all PENDING orders (they transition to CANCELLED — this is
           IRREVERSIBLE. They cannot be re-used. Run portfolio_construct again
           if a fresh batch is needed.)
         → Report: "Execution cancelled. Orders have been CANCELLED (not staged)."
         → Stop
```

---

## Programmatic usage

```python
import os
from execution.oms.order_manager import OrderManager
from execution.oms.compliance import ComplianceEngine
from execution.brokers.ibkr import IBKRBroker
from risk.circuit_breaker import CircuitBreaker

# ── Step 1: Circuit breaker check ────────────────────────────────────────────
circuit_breaker = CircuitBreaker()
if circuit_breaker.is_open:
    print("ERROR: Circuit breaker is OPEN. Trading halted.")
    print("A human operator must call circuit_breaker.reset(operator, reason_code) (C4).")
    raise SystemExit(1)

# ── Step 2: Connect to broker ────────────────────────────────────────────────
# IBKR_PORT env var: 7497 = paper, 7496 = live (C9)
broker = IBKRBroker()  # reads IBKR_HOST, IBKR_PORT from env
broker.connect()

# ── Step 3: Run compliance ───────────────────────────────────────────────────
context = {
    "circuit_breaker_open": circuit_breaker.is_open,
    "current_weights": current_weights,
    "total_nav": nav,
    "max_position_weight": 0.05,
    "max_sector_weight": 0.25,
    "sector_map": sector_map,
    "min_order_notional": 100.0,
}
om = OrderManager(broker=broker)
approved, rejected = om.run_compliance(context)

if rejected:
    print(f"⚠ {len(rejected)} orders rejected by compliance:")
    for o in rejected:
        print(f"  {o.ticker}: {o.rejection_reason}")

# ── Step 4: Display and confirm (C1) ─────────────────────────────────────────
import pandas as pd
rows = om.pending_orders_display()
print("\n=== PENDING ORDERS ===")
print(pd.DataFrame(rows).to_string(index=False))
print(f"\nTotal: {len(rows)} orders")
print()

# This is where Claude must pause and get explicit "YES" from the operator.
# NEVER proceed without this.
confirmation = input("Type YES to submit, or anything else to cancel: ").strip()

if confirmation != "YES":
    for o in approved:
        om.cancel_order(o.order_id)
    print("Execution cancelled.")
    broker.disconnect()
    raise SystemExit(0)

# ── Step 5: Submit ────────────────────────────────────────────────────────────
submitted = om.submit_pending()
print(f"Submitted {len(submitted)} orders.")

# ── Step 6: Wait for fills ────────────────────────────────────────────────────
import time
deadline = time.time() + timeout_seconds
while time.time() < deadline:
    filled = om.reconcile_fills()
    if filled:
        for o in filled:
            print(f"FILLED: {o.ticker} {o.side.value} {o.filled_quantity} @ {o.avg_fill_price:.2f}")
    remaining = [o for o in submitted if not o.is_terminal]
    if not remaining:
        break
    time.sleep(2)

broker.disconnect()
```

---

## Safety notes

| Rule | Enforcement |
|------|-------------|
| **C1** | Operator must type exact string "YES" before submit_pending() is called |
| **C4** | Circuit breaker checked before any compliance or submission |
| **C8** | IBKRBroker validates PAPER_RUN_CLEARED env var before live connection |
| **C9** | Live vs. paper controlled entirely by IBKR_PORT env var; never hardcoded |

**If in doubt, cancel.** It is always safe to cancel and re-run after investigation.
Partial fills should be logged and reconciled before the next rebalance.
