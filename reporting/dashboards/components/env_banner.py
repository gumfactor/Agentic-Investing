"""Environment banner — rendered at the top of every dashboard page.

Shows whether the operator is in PAPER or LIVE trading mode, with the
IBKR port number.  Cannot be dismissed or hidden (PRD F7.4).
"""
from __future__ import annotations

import os

import streamlit as st


def render_env_banner() -> None:
    paper = os.environ.get("PAPER_TRADING", "").lower() == "true"
    port = os.environ.get("IBKR_PORT", "")

    if paper and port == "7497":
        st.success("PAPER TRADING ACTIVE — Port 7497")
    elif not paper and port == "7496":
        st.error("LIVE TRADING ACTIVE — Port 7496")
    else:
        st.error("ENVIRONMENT MISCONFIGURED — Check PAPER_TRADING and IBKR_PORT")
