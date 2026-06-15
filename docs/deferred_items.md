# Deferred Items

Features and improvements that are intentionally deferred. No rush — revisit
during Phase 5 or when the relevant subsystem is being built out.

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

## Wash-Sale Compliance Check

`execution/oms/compliance.py::_check_wash_sale` is a working implementation
but a dead letter: nothing in the system populates `ctx["recent_loss_buys"]`.

The check requires a trade-history store that tracks realized-loss buy events
per ticker per account.  This is Phase 5 scope (reporting / audit trail module).

**Phase 5 action:** Build `execution/oms/trade_history.py` — an append-only
store (Alembic migration + C3-safe) that records fills with P&L.
`recent_loss_buys` is then populated from fills where realized_pnl < 0
within the last 30 days.

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
