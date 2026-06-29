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
        st.markdown(
            '<div style="background-color:#198754;color:white;padding:10px 16px;'
            'border-radius:4px;font-weight:bold;text-align:center;margin-bottom:12px">'
            "PAPER TRADING ACTIVE — Port 7497</div>",
            unsafe_allow_html=True,
        )
    elif not paper and port == "7496":
        st.markdown(
            '<div style="background-color:#dc3545;color:white;padding:10px 16px;'
            'border-radius:4px;font-weight:bold;text-align:center;margin-bottom:12px">'
            "⚠ LIVE TRADING ACTIVE — Port 7496</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="background-color:#dc3545;color:white;padding:10px 16px;'
            'border-radius:4px;font-weight:bold;text-align:center;margin-bottom:12px">'
            "⚠ ENVIRONMENT MISCONFIGURED — Check PAPER_TRADING and IBKR_PORT</div>",
            unsafe_allow_html=True,
        )
