"""Shared Streamlit session-state initialization for dashboard pages."""
from __future__ import annotations

import os
import uuid

import streamlit as st


def init_dashboard_session() -> None:
    """Ensure common dashboard session keys exist on every page."""
    if "session_id" not in st.session_state:
        st.session_state["session_id"] = str(uuid.uuid4())
    if "operator_email" not in st.session_state:
        st.session_state["operator_email"] = os.environ.get(
            "OPERATOR_EMAIL", "unknown"
        )
