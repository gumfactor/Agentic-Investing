"""IBKRBroker wrapper for dashboard use.

Currently a stub — the dashboard does not require a direct IBKR connection.
All position/NAV data comes from the DB (portfolio_snapshots table populated
by the Airflow DAG).
"""
from __future__ import annotations


def get_ibkr_broker():
    """Returns None — direct IBKR connection not yet wired to dashboard."""
    return None
