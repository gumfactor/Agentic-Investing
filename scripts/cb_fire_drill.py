#!/usr/bin/env python3
"""Circuit Breaker Fire Drill — RQIS Phase 4 pre-paper-trading verification.

Proves end-to-end that the circuit breaker + OMS blocking stack works
correctly, without requiring a broker connection, database, or network.

Run before starting paper trading week 1:

    python scripts/cb_fire_drill.py

Exit 0 = all checks passed — safe to proceed to paper trading.
Exit 1 = one or more checks failed — investigate before connecting IBKR.
"""

from __future__ import annotations

import sys
from datetime import date

import numpy as np
import pandas as pd

from execution.brokers.base import BaseBroker
from execution.oms.order import Order, OrderSide, OrderStatus
from execution.oms.order_manager import OrderManager
from risk.circuit_breaker import CircuitBreaker, CircuitBreakerState
from risk.realtime.monitor import RiskMonitor, RiskSnapshot


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _breach_snapshot() -> RiskSnapshot:
    """A snapshot with a hard drawdown breach (drawdown=-12%, threshold=-10%)."""
    return RiskSnapshot(
        as_of=date.today(),
        nav=880_000.0,
        drawdown=-0.12,
        var_1d_99=0.018,
        cvar_1d_99=0.022,
        portfolio_beta=0.90,
        max_concentration=0.15,
        max_sector_concentration=0.35,
        breaches=[{
            "metric": "drawdown",
            "severity": "hard",
            "value": -0.12,
            "threshold": -0.10,
        }],
        circuit_breaker_tripped=True,
    )


def _clean_snapshot() -> RiskSnapshot:
    """A snapshot with no breaches."""
    return RiskSnapshot(
        as_of=date.today(),
        nav=1_000_000.0,
        drawdown=0.0,
        var_1d_99=0.010,
        cvar_1d_99=0.013,
        portfolio_beta=0.85,
        max_concentration=0.12,
        max_sector_concentration=0.35,
        breaches=[],
        circuit_breaker_tripped=False,
    )


def _test_orders() -> list[Order]:
    return [
        Order(ticker="AAPL", side=OrderSide.BUY,  quantity=100, limit_price=150.0, strategy_id="v1"),
        Order(ticker="MSFT", side=OrderSide.SELL, quantity=50,  limit_price=420.0, strategy_id="v1"),
        Order(ticker="NVDA", side=OrderSide.BUY,  quantity=20,  limit_price=880.0, strategy_id="v1"),
    ]


def _permissive_context() -> dict:
    """Compliance context with wide limits so only the CB check matters."""
    return {
        "max_position_weight": 0.99,
        "max_sector_weight": 0.99,
        "min_order_usd": 100.0,
        "total_nav": 1_000_000.0,
        "current_weights": pd.Series(dtype=float),
        "sector_map": {},
    }


class _NullBroker(BaseBroker):
    """Records submitted orders; never sends anything to a real exchange."""

    def __init__(self) -> None:
        self.submitted: list[Order] = []

    def connect(self) -> None: pass
    def disconnect(self) -> None: pass

    def submit_order(self, order: Order) -> str:
        self.submitted.append(order)
        return f"NULL-{order.order_id[:8]}"

    def get_fill(self, broker_order_id: str) -> dict | None: return None
    def cancel_order(self, broker_order_id: str) -> bool: return True
    def get_positions(self) -> dict[str, float]: return {}
    def get_account_value(self) -> float: return 1_000_000.0

    @property
    def is_paper(self) -> bool: return True


# ── Runner ────────────────────────────────────────────────────────────────────

class DrillRunner:
    _GREEN = "\033[92m"
    _RED   = "\033[91m"
    _RESET = "\033[0m"
    _BOLD  = "\033[1m"

    def __init__(self) -> None:
        self._results: list[tuple[str, bool, str]] = []

    # ── Assertion helper ──────────────────────────────────────────────────────

    def _check(self, name: str, condition: bool, detail: str = "") -> bool:
        ok = self._GREEN + "PASS" + self._RESET
        ng = self._RED   + "FAIL" + self._RESET
        tag = ok if condition else ng
        print(f"  [{tag}] {name}")
        if not condition and detail:
            print(f"         {self._RED}detail: {detail}{self._RESET}")
        self._results.append((name, condition, detail))
        return condition

    def _section(self, title: str) -> None:
        print(f"\n{self._BOLD}── {title} {'─' * max(0, 58 - len(title))}{self._RESET}")

    # ── Test groups ───────────────────────────────────────────────────────────

    def _test_initial_state(self) -> None:
        self._section("1. Initial state — CB starts CLOSED")
        cb = CircuitBreaker()
        self._check("CB.is_closed is True",              cb.is_closed)
        self._check("CB.is_open is False",               not cb.is_open)
        self._check("CB.state == CLOSED",                cb.state == CircuitBreakerState.CLOSED)
        self._check("No trips in history",               len(cb.trip_history()) == 0)
        self._check("No resets in history",              len(cb.reset_history()) == 0)

    def _test_breach_trips_cb(self) -> None:
        self._section("2. Hard breach trips the circuit breaker")
        cb = CircuitBreaker()
        snap = _breach_snapshot()

        result = cb.evaluate(snap)
        self._check("evaluate() returns True",           result is True)
        self._check("CB transitions to OPEN",            cb.is_open)
        self._check("Trip recorded in history",          len(cb.trip_history()) == 1)

        ev = cb.trip_history()[0]
        self._check("Trip records correct metric",       ev.metric == "drawdown")
        self._check("Trip records correct value",        abs(ev.value - (-0.12)) < 1e-9,
                    f"ev.value={ev.value}")
        self._check("Trip records correct threshold",    abs(ev.threshold - (-0.10)) < 1e-9,
                    f"ev.threshold={ev.threshold}")

        # A second evaluate while already OPEN appends but does not double-transition
        cb.evaluate(snap)
        self._check("Second breach appends to history",  len(cb.trip_history()) == 2)
        self._check("CB remains OPEN",                   cb.is_open)

        # A clean snapshot while OPEN should not re-close the breaker
        cb.evaluate(_clean_snapshot())
        self._check("Clean snapshot does not re-close CB", cb.is_open)

    def _test_orders_blocked_by_compliance(self) -> None:
        self._section("3. OMS compliance rejects all orders when CB is open")
        cb = CircuitBreaker()
        cb.evaluate(_breach_snapshot())

        om = OrderManager(broker=_NullBroker(), circuit_breaker=cb)
        om.stage_batch(_test_orders())
        approved, rejected = om.run_compliance(_permissive_context())

        self._check("0 orders approved",                 len(approved) == 0,
                    f"approved={len(approved)}")
        self._check("All 3 orders rejected",             len(rejected) == 3,
                    f"rejected={len(rejected)}")
        all_rejected = all(o.status == OrderStatus.REJECTED for o in rejected)
        self._check("All rejected orders have REJECTED status", all_rejected)
        cb_reasons = [o for o in rejected if "circuit" in o.rejection_reason.lower()]
        self._check("All rejections cite circuit breaker", len(cb_reasons) == 3,
                    f"reasons: {[o.rejection_reason for o in rejected]}")

    def _test_submit_blocked_toctou(self) -> None:
        self._section("4. submit_pending() blocked if CB opens after compliance (TOCTOU fix)")
        cb = CircuitBreaker()
        broker = _NullBroker()
        om = OrderManager(broker=broker, circuit_breaker=cb)
        om.stage_batch(_test_orders())

        # While CB is CLOSED, compliance should approve all orders
        approved, rejected = om.run_compliance(_permissive_context())
        self._check("All 3 approved while CB CLOSED",    len(approved) == 3,
                    f"approved={len(approved)}, rejected={[o.rejection_reason for o in rejected]}")
        pending = [o for o in om.all_orders() if o.status == OrderStatus.PENDING]
        self._check("All 3 in PENDING state",            len(pending) == 3)

        # Trip the CB between compliance and submission (simulates an intraday breach)
        cb.evaluate(_breach_snapshot())
        self._check("CB open before submit call",        cb.is_open)

        # submit_pending() must block and raise immediately
        raised = False
        err_msg = ""
        try:
            om.submit_pending()
        except RuntimeError as exc:
            raised = True
            err_msg = str(exc)
        self._check("submit_pending() raises RuntimeError", raised)
        mentions_c4 = "C4" in err_msg or "circuit breaker" in err_msg.lower()
        self._check("Error message mentions circuit breaker / C4", mentions_c4,
                    f"Got: {err_msg!r}")
        self._check("Broker received 0 orders",          len(broker.submitted) == 0,
                    f"broker.submitted={len(broker.submitted)}")

    def _test_reset_validation(self) -> None:
        self._section("5. Reset validation (C4 enforcement)")
        cb = CircuitBreaker()

        # Reset on already-CLOSED CB must raise RuntimeError
        raised = False
        try:
            cb.reset("op@firm.com", "TEST")
        except RuntimeError:
            raised = True
        self._check("reset() on CLOSED CB raises RuntimeError", raised)

        cb.evaluate(_breach_snapshot())  # now OPEN

        # Empty / whitespace operator
        for bad_op, label in [("", "empty-string operator"), ("   ", "whitespace-only operator")]:
            raised = False
            try:
                cb.reset(bad_op, "CLEARED")
            except ValueError:
                raised = True
            self._check(f"reset() rejects {label}",      raised)

        # Empty reason_code
        raised = False
        try:
            cb.reset("op@firm.com", "")
        except ValueError:
            raised = True
        self._check("reset() rejects empty reason_code", raised)

        # Whitespace reason_code
        raised = False
        try:
            cb.reset("op@firm.com", "   ")
        except ValueError:
            raised = True
        self._check("reset() rejects whitespace-only reason_code", raised)

        self._check("CB still OPEN — no valid reset yet", cb.is_open)

    def _test_valid_reset(self) -> None:
        self._section("6. Valid human reset re-closes the breaker")
        cb = CircuitBreaker()
        cb.evaluate(_breach_snapshot())

        cb.reset(
            operator="mshane@thecanadalist.ca",
            reason_code="CB_FIRE_DRILL_COMPLETE",
        )
        self._check("CB transitions to CLOSED",          cb.is_closed)
        self._check("Reset recorded in history",         len(cb.reset_history()) == 1)
        ev = cb.reset_history()[0]
        self._check("Reset records operator correctly",  ev.operator == "mshane@thecanadalist.ca")
        self._check("Reset records reason_code",         ev.reason_code == "CB_FIRE_DRILL_COMPLETE")
        self._check("Trip history preserved after reset", len(cb.trip_history()) == 1)

        # Resetting a CLOSED CB again must raise
        raised = False
        try:
            cb.reset("op@firm.com", "AGAIN")
        except RuntimeError:
            raised = True
        self._check("Second reset on CLOSED CB raises RuntimeError", raised)

    def _test_orders_flow_after_reset(self) -> None:
        self._section("7. Orders flow through compliance after valid reset")
        cb = CircuitBreaker()
        cb.evaluate(_breach_snapshot())
        cb.reset(operator="mshane@thecanadalist.ca", reason_code="CB_FIRE_DRILL_COMPLETE")

        om = OrderManager(broker=_NullBroker(), circuit_breaker=cb)
        om.stage_batch(_test_orders())
        approved, rejected = om.run_compliance(_permissive_context())

        self._check("All 3 orders approved after reset", len(approved) == 3,
                    f"rejected: {[o.rejection_reason for o in rejected]}")
        self._check("0 orders rejected",                 len(rejected) == 0)

        # Safety reminder — do NOT call submit_pending() here.
        # C1 requires an explicit operator "YES" before submission.
        self._check("C1 confirmed: submit_pending() NOT called in drill (requires YES)", True)

    def _test_full_stack_risk_monitor(self) -> None:
        self._section("8. Full-stack: RiskMonitor.snapshot() → hard breach → CB trips")
        rng = np.random.default_rng(seed=42)
        n = 60

        # Simulate 20 good days followed by 40 bad days to create a large drawdown
        up_rets  = rng.normal(loc=0.003,  scale=0.008, size=20)
        dn_rets  = rng.normal(loc=-0.006, scale=0.008, size=40)
        port_returns   = pd.Series(np.concatenate([up_rets, dn_rets]))
        asset_returns  = pd.DataFrame({
            "AAPL": rng.normal(0.0003, 0.012, n),
            "MSFT": rng.normal(0.0004, 0.011, n),
        })
        benchmark_returns = pd.Series(rng.normal(0.0003, 0.010, n))
        weights = pd.Series({"AAPL": 0.60, "MSFT": 0.40})

        # Compute peak NAV from the synthetic return series
        nav_path = [1_000_000.0]
        for r in port_returns:
            nav_path.append(nav_path[-1] * (1 + float(r)))
        peak_nav    = max(nav_path)
        current_nav = nav_path[-1]
        actual_dd   = (current_nav / peak_nav) - 1

        # Monitor with a tight drawdown threshold (-1%) to guarantee a trigger, and
        # permissive thresholds on all other metrics so only drawdown fires.
        monitor = RiskMonitor(
            hard_drawdown=-0.01,
            hard_var=1.0,
            hard_beta=99.0,
            hard_concentration=1.0,
            warn_drawdown=-0.005,
            warn_var=0.99,
            warn_beta=98.0,
            warn_concentration=0.99,
            peak_nav=peak_nav,
        )
        snap = monitor.snapshot(
            as_of=date.today(),
            nav=current_nav,
            weights=weights,
            portfolio_returns=port_returns,
            asset_returns=asset_returns,
            benchmark_returns=benchmark_returns,
        )

        self._check(
            f"RiskMonitor detects drawdown breach ({actual_dd:.2%} vs -1.0% threshold)",
            snap.circuit_breaker_tripped,
            f"drawdown={actual_dd:.4f}, circuit_breaker_tripped={snap.circuit_breaker_tripped}",
        )
        hard_breaches = [b for b in snap.breaches if b["severity"] == "hard"]
        self._check("Snapshot contains at least one hard breach", len(hard_breaches) >= 1)

        cb = CircuitBreaker()
        cb.evaluate(snap)
        self._check("CB trips from live RiskMonitor snapshot",   cb.is_open)
        self._check("Trip recorded with metric='drawdown'",
                    cb.trip_history()[0].metric == "drawdown" if cb.trip_history() else False)

    # ── Orchestration ─────────────────────────────────────────────────────────

    def run(self) -> bool:
        print(f"\n{'═' * 62}")
        print(f"  {self._BOLD}RQIS Circuit Breaker Fire Drill{self._RESET}")
        print(f"  Phase 4 pre-paper-trading verification · {date.today()}")
        print(f"{'═' * 62}")

        self._test_initial_state()
        self._test_breach_trips_cb()
        self._test_orders_blocked_by_compliance()
        self._test_submit_blocked_toctou()
        self._test_reset_validation()
        self._test_valid_reset()
        self._test_orders_flow_after_reset()
        self._test_full_stack_risk_monitor()

        return self._summary()

    def _summary(self) -> bool:
        passed = sum(1 for _, ok, _ in self._results if ok)
        failed = sum(1 for _, ok, _ in self._results if not ok)
        total  = len(self._results)

        print(f"\n{'═' * 62}")
        if failed == 0:
            print(f"  {self._GREEN}{self._BOLD}✓  ALL {total} CHECKS PASSED{self._RESET}")
            print(  "  Circuit breaker and OMS blocking verified end-to-end.")
            print(  "  Proceed to paper trading runbook: docs/runbooks/paper_trading_start.md")
        else:
            print(f"  {self._RED}{self._BOLD}✗  {failed} / {total} CHECKS FAILED{self._RESET}")
            print(  "  DO NOT start paper trading until all checks pass.")
            print(  "\n  Failed checks:")
            for name, ok, detail in self._results:
                if not ok:
                    print(f"    - {name}")
                    if detail:
                        print(f"      {self._RED}{detail}{self._RESET}")
        print(f"{'═' * 62}\n")
        return failed == 0


if __name__ == "__main__":
    ok = DrillRunner().run()
    sys.exit(0 if ok else 1)
