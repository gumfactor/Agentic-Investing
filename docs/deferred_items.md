# Deferred Items

Features and improvements that are intentionally deferred. No rush — revisit
during Phase 5 or when the relevant subsystem is being built out.

For defects, safety issues, and implementation weaknesses that must be triaged as fixes, use `bugs.md` as the canonical running tally. `bugs.md` classifies each known fix by category, severity, implementation priority, and short/medium/long-term horizon. This file remains for intentionally deferred feature work or design debt; if a deferred item is also a concrete bug, cross-reference its `BUG-XXX` entry in `bugs.md`.

---

## Portfolio Constraints — Unimplemented Fields

`PortfolioConstraints` declares the following fields but nothing in the
optimizer or compliance engine currently reads them.  They are placeholders
for Phase 5 enforcement.

| Field | Intent | Where to enforce |
|-------|--------|-----------------|
| `target_volatility` | Scale weights so portfolio vol hits this target | MVOOptimizer / RiskParityOptimizer post-processing |
| `max_portfolio_beta` | Block orders when post-trade portfolio beta exceeds limit | ComplianceEngine._check_portfolio_beta (new check) |
| `factor_bounds` | Constrain factor exposures (value, momentum, quality) | MVOOptimizer — add CVXPY constraints from factor loading matrix |
| `min_adv_fraction` | Limit order size to X% of 30-day average daily volume | ComplianceEngine._check_liquidity (new check, needs ADV data) |

**Phase 5 action:** Implement each field one at a time; start with
`target_volatility` (pure math, no data dependency) then `max_portfolio_beta`
(needs SPY beta from RiskMonitor), then `min_adv_fraction` (needs ADV feed).

---

## ~~Wash-Sale Compliance Check~~ [RESOLVED — 2026-06-23, Session 41]

`execution/oms/trade_history.py` was built in Session 41 (branch
`claude/trade-journal`, commits `708c35a`, `bf2098f`).  The
`TradeJournal.wash_sale_context()` method now populates `ctx["recent_loss_buys"]`
from real fill history, and `OrderManager.run_compliance()` auto-injects it when
a `TradeJournal` is provided.  `_check_wash_sale` is no longer a dead letter.
The Alembic migration is `infra/db/migrations/versions/004_trade_journal_schema.py`.

---

## Sector Concentration Compliance Check

`execution/oms/compliance.py::_check_sector_concentration` is implemented
and tested, but `ctx["sector_map"]` and `ctx["sector_weights"]` are never
populated in live execution contexts — the check always passes.

Requires a GICS sector mapping data source (static YAML or database table).

**Phase 5 action:**
1. Create `config/sector_map.yaml` mapping ticker → GICS sector (can be
   bootstrapped from yfinance `Ticker.info["sector"]`).
2. Load it in the execution pipeline and populate `ctx["sector_map"]`.
3. Compute `ctx["sector_weights"]` from current portfolio weights + sector_map
   before calling `run_compliance()`.

---

## Circuit Breaker — Audit Trail Persistence

`risk/circuit_breaker.py` stores `_trip_history` and `_reset_history` in
memory only.  A process restart loses the entire audit trail.

**Phase 5 action:** Add an Alembic migration for an append-only
`circuit_breaker_events` table (C3-safe — no UPDATE/DELETE).  Persist
TripEvent and ResetEvent on write.  Load history from DB on startup.

---

## Stuck PENDING Orders — No Retry Counter or Expiry

`OrderManager.submit_pending()` catches `(ConnectionError, TimeoutError, OSError)` and
leaves the order in PENDING for retry.  There is no retry counter, no age-out timer,
and no operator alert when an order has been PENDING for an extended period.

Risks:
1. A stale PENDING order (from a prior session's failed submission) may be re-submitted
   after market conditions have changed materially.
2. If `placeOrder()` reached the broker before the socket error, the retry will submit
   a duplicate order.

**Phase 5 action:** Add `max_retries: int` and `pending_expiry_seconds: int` fields to
OrderManager.  Track a per-order retry counter and first-pending timestamp.  After N
retries (or after T seconds), transition to REJECTED and emit an `ERROR`-level alert.
For live trading: before retrying, check `get_positions()` to verify the position was
not already filled from an earlier attempt.

---

## IBKRBroker — port=0 Treated as Falsy

`execution/brokers/ibkr.py` line 65:
```python
raw_port = port or int(os.environ.get("IBKR_PORT", "7497"))
```
If `port=0` is explicitly passed, it is treated as falsy and the env var branch is taken,
silently ignoring the explicit argument.  Unlikely in practice (port 0 is not a valid
IB port) but latent API confusion.

**Phase 5 action:** Change to `port if port is not None else int(os.environ.get(...))`.

---

## Monthly Rebalance Trigger — Implicit min_holding_days Dependency

`portfolio/rebalancing/trigger.py`: for `MONTHLY` frequency, `_is_calendar_rebalance_day`
checks `today.month != self._last_rebalance_date.month` but does NOT explicitly check
`days_since >= min_holding_days`.  The outer `should_rebalance` guard catches this for
the live path, but in test/backtest contexts where `_trading_days_since` is not correctly
maintained via `advance_day()`, a monthly rebalance can fire after 0 trading days.

**Phase 5 action:** Add an explicit `and days_since >= self._min_holding_days` guard
inside `_is_calendar_rebalance_day` for `MONTHLY` (matching the `WEEKLY` pattern).
