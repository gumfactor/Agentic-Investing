"""Page 3 — Risk Monitor.

Real-time risk metrics. The page that should alarm you.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import exc as sa_exc

from reporting.dashboards.components.circuit_breaker import (
    get_alert_manager,
    get_circuit_breaker,
    render_circuit_breaker_sidebar,
    render_circuit_breaker_warning,
)
from reporting.dashboards.components.env_banner import render_env_banner
from reporting.dashboards.db import get_engine
from reporting.dashboards.queries import (
    active_strategy_id,
    daily_returns_for_tickers,
    latest_portfolio_snapshot,
)

st.set_page_config(page_title="Risk Monitor — RQIS", page_icon="🛡️", layout="wide")

render_env_banner()
render_circuit_breaker_warning()
render_circuit_breaker_sidebar()

# Auto-refresh during market hours only
try:
    from streamlit_autorefresh import st_autorefresh
    now_utc = datetime.now(timezone.utc)
    # Market hours: 09:30–16:00 ET (roughly 13:30–20:00 UTC, ignoring DST edge cases)
    market_open = time(13, 30)
    market_close = time(20, 0)
    now_time = now_utc.time()
    if market_open <= now_time <= market_close and now_utc.weekday() < 5:
        st_autorefresh(interval=30_000, key="risk_refresh")
except ImportError:
    pass

st.title("Risk Monitor")

engine = get_engine()

# ── Strategy selector ───────────────────────────────────────────────────────

try:
    active_sid = active_strategy_id(engine)
except (sa_exc.SQLAlchemyError, OSError):
    active_sid = None

strategy_id = st.sidebar.text_input(
    "Strategy ID", value=active_sid or "v1_base_momentum"
)

# ── Compute risk snapshot ───────────────────────────────────────────────────

try:
    snap = latest_portfolio_snapshot(engine, strategy_id)
except (sa_exc.SQLAlchemyError, OSError):
    snap = None

if snap is None:
    st.info("No portfolio data available. Risk metrics require a portfolio snapshot.")
    st.stop()

nav = float(snap["nav_usd"])
positions_raw = snap["positions"]
if isinstance(positions_raw, str):
    positions_raw = json.loads(positions_raw)

if not positions_raw or nav <= 0:
    st.info("Portfolio is empty or NAV is zero. Risk metrics require positions.")
    st.stop()

# Build weights
tickers = [p["ticker"] for p in positions_raw]
quantities = {p["ticker"]: float(p.get("quantity", 0)) for p in positions_raw}
prices = {
    p["ticker"]: float(p.get("current_price", 0)) for p in positions_raw
    if p.get("current_price")
}

mkt_values = {t: quantities[t] * prices.get(t, 0) for t in tickers}
total_mkt = sum(mkt_values.values())
weights_dict = {t: mkt_values[t] / total_mkt for t in tickers} if total_mkt > 0 else {}
weights_series = pd.Series(weights_dict)

# Fetch daily returns
try:
    all_tickers = tickers + ["SPY"]
    returns_df = daily_returns_for_tickers(engine, all_tickers, lookback_days=252)

    if returns_df.empty or len(returns_df) < 30:
        st.warning("Insufficient price history for risk computation (need 30+ days).")
        risk_snap = None
    else:
        benchmark_rets = returns_df["SPY"] if "SPY" in returns_df.columns else pd.Series(dtype=float)
        asset_rets = returns_df[[c for c in returns_df.columns if c != "SPY" and c in tickers]]

        # Portfolio returns from weighted asset returns
        common = [t for t in tickers if t in asset_rets.columns and t in weights_dict]
        if common:
            w = pd.Series({t: weights_dict[t] for t in common})
            port_rets = asset_rets[common].fillna(0).dot(w)
        else:
            port_rets = pd.Series(dtype=float)

        from risk.realtime.monitor import RiskMonitor
        monitor = RiskMonitor()
        risk_snap = monitor.snapshot(
            as_of=date.today(),
            nav=nav,
            weights=weights_series,
            portfolio_returns=port_rets,
            asset_returns=asset_rets,
            benchmark_returns=benchmark_rets,
        )
except (sa_exc.SQLAlchemyError, OSError, Exception) as exc:
    st.warning(f"Risk computation failed: {type(exc).__name__}: {exc}")
    risk_snap = None

# ── Risk metric cards ───────────────────────────────────────────────────────

st.subheader("Risk Metrics")

if risk_snap is not None:
    # Hard breach banner
    hard_breaches = [b for b in risk_snap.breaches if b["severity"] == "hard"]
    for breach in hard_breaches:
        st.error(
            f"HARD BREACH: **{breach['metric']}** = {breach['value']:.4f} "
            f"(threshold: {breach['threshold']:.4f})"
        )

    col1, col2, col3 = st.columns(3)
    col1.metric("1-day VaR (99%)", f"{risk_snap.var_1d_99:.2%}")
    col2.metric("CVaR (99%)", f"{risk_snap.cvar_1d_99:.2%}")
    col3.metric("Drawdown", f"{risk_snap.drawdown:.2%}")

    col4, col5, col6 = st.columns(3)
    col4.metric("Portfolio Beta", f"{risk_snap.portfolio_beta:.3f}")
    col5.metric("Max Concentration", f"{risk_snap.max_concentration:.2%}")
    col6.metric("Max Sector Conc.", f"{risk_snap.max_sector_concentration:.2%}")

    st.caption(
        f"As of: {risk_snap.as_of}  |  NAV: ${risk_snap.nav:,.2f}  |  "
        f"Breaches: {len(risk_snap.breaches)}"
    )
else:
    st.info("Risk metrics could not be computed. Check data availability.")

# ── Alert feed ──────────────────────────────────────────────────────────────

st.subheader("Alerts")

am = get_alert_manager()
unacked = am.unacknowledged()

if not unacked:
    st.success("No unacknowledged alerts.")
else:
    for alert in unacked:
        acol1, acol2 = st.columns([4, 1])
        if alert.severity == "hard":
            acol1.error(
                f"**{alert.metric}** = {alert.value:.4f} "
                f"(threshold: {alert.threshold:.4f}) — {alert.fired_at:%H:%M UTC}"
            )
        else:
            acol1.warning(
                f"**{alert.metric}** = {alert.value:.4f} "
                f"(threshold: {alert.threshold:.4f}) — {alert.fired_at:%H:%M UTC}"
            )
        if acol2.button("Ack", key=f"ack_{alert.alert_id}"):
            am.acknowledge(alert.alert_id)
            st.rerun()

# Acknowledged alerts in expander
all_alerts = am.all_alerts()
acked = [a for a in all_alerts if a.acknowledged]
if acked:
    with st.expander(f"Acknowledged Alerts ({len(acked)})"):
        for alert in acked[-20:]:
            st.caption(
                f"{alert.fired_at:%Y-%m-%d %H:%M} — {alert.severity}: "
                f"{alert.metric} = {alert.value:.4f}"
            )

# ── Circuit breaker controls ────────────────────────────────────────────────

st.divider()
st.subheader("Circuit Breaker")

cb = get_circuit_breaker()

if cb.is_closed:
    st.success("Circuit Breaker: CLOSED — trading is enabled.")
else:
    st.error("Circuit Breaker: OPEN — trading is halted.")

    # Trip history
    trips = cb.trip_history()
    if trips:
        trip_rows = [
            {
                "Metric": t.metric,
                "Value": f"{t.value:.4f}",
                "Tripped At": t.tripped_at.strftime("%Y-%m-%d %H:%M UTC"),
            }
            for t in trips
        ]
        st.dataframe(trip_rows, use_container_width=True, hide_index=True)

    # Reset form (C4 + C9 enforcement)
    st.subheader("Reset Circuit Breaker")

    operator_id = st.text_input(
        "Operator ID",
        value=st.session_state.get("operator_email", ""),
        key="cb_reset_operator",
    )
    reason_code = st.text_input(
        "Reason code (min 10 characters)",
        key="cb_reset_reason",
    )

    reason_valid = len(reason_code.strip()) >= 10
    operator_valid = len(operator_id.strip()) > 0

    if not reason_valid and reason_code:
        st.caption("Reason code must be at least 10 characters.")

    if st.button(
        "Request Reset",
        disabled=not (reason_valid and operator_valid),
    ):
        st.session_state["cb_reset_pending"] = True

    if st.session_state.get("cb_reset_pending", False):
        st.warning(
            "This will re-enable order submission. Are you sure?"
        )
        if st.button("Confirm Reset", type="primary", key="cb_confirm_reset"):
            try:
                cb.reset(
                    operator=operator_id.strip(),
                    reason_code=reason_code.strip(),
                )
                st.session_state["cb_reset_pending"] = False
                st.success("Circuit breaker reset successfully.")
                st.rerun()
            except ValueError as e:
                st.error(f"Reset failed: {e}")
