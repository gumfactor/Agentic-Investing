"""SQLAlchemy engine factory for the Streamlit dashboard.

Uses @st.cache_resource to ensure one engine per Streamlit process,
not per page rerun or per tab.
"""
from __future__ import annotations

import os

import streamlit as st
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


@st.cache_resource
def get_engine() -> Engine:
    url = os.environ["DATABASE_URL"]
    return create_engine(url, pool_size=5, max_overflow=2, pool_pre_ping=True)
