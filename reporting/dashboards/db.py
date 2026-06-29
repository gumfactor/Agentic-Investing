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
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "The dashboard requires a PostgreSQL connection string."
        )
    return create_engine(url, pool_size=5, max_overflow=2, pool_pre_ping=True)
