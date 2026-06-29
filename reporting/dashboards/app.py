"""RQIS Streamlit Dashboard — entry point.

Launch with:
    streamlit run reporting/dashboards/app.py
"""
from __future__ import annotations

import os

import streamlit as st

from reporting.dashboards.components.circuit_breaker import (
    render_circuit_breaker_sidebar,
    render_circuit_breaker_warning,
)
from reporting.dashboards.components.env_banner import render_env_banner
from reporting.dashboards.session import init_dashboard_session

# -- Page config (must be first Streamlit command) --
st.set_page_config(
    page_title="RQIS Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -- Session state init (Section 5.4) --
init_dashboard_session()

# -- Shared components (every page) --
render_env_banner()
render_circuit_breaker_warning()

# -- Sidebar --
st.sidebar.title("RQIS Dashboard")
render_circuit_breaker_sidebar()

# -- Pending blotter badge (Section 5.3) --
_artifact_dir = os.environ.get("RQIS_PAPER_ARTIFACT_DIR", "")
if _artifact_dir:
    from pathlib import Path

    from reporting.dashboards.db import get_engine
    from reporting.dashboards.queries import pending_blotter

    _pending = pending_blotter(Path(_artifact_dir), get_engine())
    if _pending:
        st.sidebar.markdown(
            ':red[● Blotter awaiting approval] — go to **Blotter Approval**'
        )

st.sidebar.divider()
st.sidebar.caption(
    f"Session: {st.session_state['session_id'][:8]}  \n"
    f"Operator: {st.session_state['operator_email']}"
)

# -- Landing page content --
st.title("RQIS — Robust Quant Investment System")
st.markdown(
    "Use the sidebar to navigate to dashboard pages.  \n"
    "**Page 4 — Blotter Approval** is the C1 gate for order submission."
)
