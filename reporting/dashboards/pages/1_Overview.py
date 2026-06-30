"""Page 1 — Overview.

Immediate system state at a glance. Designed for the 23:05 ET check:
"Did the pipeline run? Is approval needed?"
"""
from __future__ import annotations

import os
from pathlib import Path

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
    latest_portfolio_snapshot,
    nav_history,
    pending_blotter,
    pipeline_health,
    previous_portfolio_snapshot,
)

st.set_page_config(page_title="Overview — RQIS", page_icon="📊", layout="wide")

render_env_banner()
render_circuit_breaker_warning()
render_circuit_breaker_sidebar()

try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=60_000, key="overview_refresh")
except ImportError:
    pass

st.title("Overview")

engine = get_engine()

# ── Strategy selector ───────────────────────────────────────────────────────

try:
    active_sid = active_strategy_id(engine)
except (sa_exc.SQLAlchemyError, OSError):
    active_sid = None

strategy_id = st.sidebar.text_input(
    "Strategy ID", value=active_sid or "v1_base_momentum"
)

# ── NAV metrics ─────────────────────────────────────────────────────────────

st.subheader("Portfolio")

try:
    snap = latest_portfolio_snapshot(engine, strategy_id)
    prev_snap = previous_portfolio_snapshot(engine, strategy_id)
except (sa_exc.SQLAlchemyError, OSError):
    snap = None
    prev_snap = None

if snap is None:
    st.info("No portfolio snapshot available. The Airflow DAG populates this data.")
else:
    nav = float(snap["nav_usd"])
    cash = float(snap["cash_usd"])
    positions_data = snap["positions"]
    if isinstance(positions_data, str):
        import json
        positions_data = json.loads(positions_data)
    n_positions = len(positions_data) if positions_data else 0

    prev_nav = float(prev_snap["nav_usd"]) if prev_snap else None
    nav_delta = nav - prev_nav if prev_nav else None
    nav_pct = (nav / prev_nav - 1) if prev_nav and prev_nav > 0 else None

    # Drawdown from peak
    try:
        nav_hist = nav_history(engine, strategy_id, lookback_days=3650)
        if not nav_hist.empty:
            peak_nav = float(nav_hist["nav_usd"].max())
            drawdown = (nav / peak_nav - 1.0) if peak_nav > 0 else 0.0
        else:
            drawdown = 0.0
    except (sa_exc.SQLAlchemyError, OSError):
        drawdown = 0.0

    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "NAV",
        f"${nav:,.2f}",
        delta=f"${nav_delta:,.2f}" if nav_delta is not None else None,
    )
    col2.metric("Cash", f"${cash:,.2f}")
    col3.metric("Drawdown", f"{drawdown:.2%}")
    col4.metric("Open Positions", n_positions)

    st.caption(
        f"Snapshot date: {snap['snapshot_date']}  |  "
        f"Source: {snap['source']}  |  "
        f"Strategy: {strategy_id}"
    )

# ── Pending blotter ─────────────────────────────────────────────────────────

st.subheader("Blotter Status")

artifact_dir_str = os.environ.get("RQIS_PAPER_ARTIFACT_DIR", "")
if artifact_dir_str:
    try:
        _pending = pending_blotter(Path(artifact_dir_str), engine)
        if _pending:
            run_id = _pending["blotter"].get("run_id", "unknown")
            st.warning(
                f"Blotter awaiting approval — Run ID: {run_id[:12]}. "
                "Go to **Blotter Approval** page."
            )
        else:
            st.success("No blotter awaiting approval.")
    except (sa_exc.SQLAlchemyError, OSError):
        st.caption("Unable to check pending blotter.")
else:
    st.caption("RQIS_PAPER_ARTIFACT_DIR not set — blotter detection unavailable.")

# ── Pipeline health ─────────────────────────────────────────────────────────

st.subheader("Pipeline Health")

try:
    health = pipeline_health(engine)

    rows = []
    for name, info in health.items():
        age = info["age"]
        if age is None:
            age_str = "No data"
            status = "No data"
        else:
            hours = age.total_seconds() / 3600
            if hours < 1:
                age_str = f"{int(age.total_seconds() / 60)} min ago"
            elif hours < 24:
                age_str = f"{hours:.1f} hours ago"
            else:
                age_str = f"{hours / 24:.1f} days ago"
            status = "Fresh" if info["ok"] else "Stale"
        rows.append({"Pipeline": name.title(), "Last Updated": age_str, "Status": status})

    st.dataframe(
        rows,
        column_config={
            "Status": st.column_config.TextColumn("Status"),
        },
        use_container_width=True,
        hide_index=True,
    )
except (sa_exc.SQLAlchemyError, OSError) as exc:
    st.caption(f"Pipeline health unavailable: {type(exc).__name__}")

# ── Active alerts ───────────────────────────────────────────────────────────

st.subheader("Active Alerts")

am = get_alert_manager()
unacked = am.unacknowledged()

if not unacked:
    st.success("No active alerts.")
else:
    st.metric("Unacknowledged Alerts", len(unacked))
    for alert in unacked:
        if alert.severity == "hard":
            st.error(f"**{alert.metric}** = {alert.value:.4f} (threshold: {alert.threshold:.4f})")
        else:
            st.warning(f"**{alert.metric}** = {alert.value:.4f} (threshold: {alert.threshold:.4f})")
