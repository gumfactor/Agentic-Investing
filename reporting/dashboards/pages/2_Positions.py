"""Page 2 — Positions & P&L.

What do we own, what did it cost, what is it worth.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st
from sqlalchemy import exc as sa_exc

from reporting.dashboards.components.circuit_breaker import (
    render_circuit_breaker_sidebar,
    render_circuit_breaker_warning,
)
from reporting.dashboards.components.env_banner import render_env_banner
from reporting.dashboards.db import get_engine
from reporting.dashboards.queries import (
    active_strategy_id,
    latest_alpha_scores,
    latest_portfolio_snapshot,
    latest_prices,
    realized_pnl_summary,
)

st.set_page_config(page_title="Positions — RQIS", page_icon="📈", layout="wide")

render_env_banner()
render_circuit_breaker_warning()
render_circuit_breaker_sidebar()

st.title("Positions & P&L")

engine = get_engine()

# ── Strategy selector ───────────────────────────────────────────────────────

try:
    active_sid = active_strategy_id(engine)
except (sa_exc.SQLAlchemyError, OSError):
    active_sid = None

strategy_id = st.sidebar.text_input(
    "Strategy ID", value=active_sid or "v1_base_momentum"
)

if st.sidebar.button("Refresh"):
    st.rerun()

# ── Holdings table ──────────────────────────────────────────────────────────

st.subheader("Holdings")

try:
    snap = latest_portfolio_snapshot(engine, strategy_id)
except (sa_exc.SQLAlchemyError, OSError):
    snap = None

if snap is None:
    st.info(
        "No portfolio snapshot available. Holdings will appear after the "
        "Airflow DAG runs `fetch_ibkr_snapshot`."
    )
else:
    nav = float(snap["nav_usd"])
    positions_raw = snap["positions"]
    if isinstance(positions_raw, str):
        positions_raw = json.loads(positions_raw)

    if not positions_raw:
        st.info("Portfolio has no open positions.")
    else:
        tickers = [p["ticker"] for p in positions_raw]

        # Fetch latest prices
        try:
            prices_df = latest_prices(engine, tickers)
            price_map: dict[str, float] = {}
            if not prices_df.empty:
                price_map = dict(zip(prices_df["ticker"], prices_df["close"]))
        except (sa_exc.SQLAlchemyError, OSError):
            price_map = {}

        # Fetch alpha score ranks
        try:
            scores_df = latest_alpha_scores(engine, strategy_id, limit=500)
            score_map: dict[str, tuple[int, float]] = {}
            if not scores_df.empty:
                for _, s in scores_df.iterrows():
                    score_map[s["ticker"]] = (int(s["rank"]), float(s["alpha_score"]))
        except (sa_exc.SQLAlchemyError, OSError):
            score_map = {}

        # Build display rows
        rows = []
        for pos in positions_raw:
            ticker = pos["ticker"]
            qty = float(pos.get("quantity", 0))
            avg_cost = pos.get("avg_cost")
            last_price = price_map.get(ticker)
            if last_price is not None:
                last_price = float(last_price)

            # Unrealized P&L
            if avg_cost is not None and last_price is not None:
                avg_cost = float(avg_cost)
                unrealized_pnl = (last_price - avg_cost) * qty
                unrealized_pct = (last_price / avg_cost - 1) if avg_cost > 0 else None
            else:
                unrealized_pnl = None
                unrealized_pct = None

            # Weight
            mkt_value = last_price * qty if last_price else None
            weight = (mkt_value / nav) if mkt_value and nav > 0 else None

            # Alpha score
            rank_val, score_val = score_map.get(ticker, (None, None))

            rows.append({
                "Ticker": ticker,
                "Quantity": int(qty),
                "Avg Cost": f"${avg_cost:.2f}" if avg_cost is not None else "N/A",
                "Last Price": f"${last_price:.2f}" if last_price is not None else "N/A",
                "Unrealized P&L ($)": unrealized_pnl,
                "Unrealized P&L (%)": unrealized_pct,
                "Weight (%)": weight,
                "Alpha Rank": rank_val,
                "Alpha Score": score_val,
            })

        df = pd.DataFrame(rows)

        st.dataframe(
            df,
            column_config={
                "Unrealized P&L ($)": st.column_config.NumberColumn(
                    "Unrealized P&L ($)", format="$%.2f"
                ),
                "Unrealized P&L (%)": st.column_config.NumberColumn(
                    "Unrealized P&L (%)", format="%.2%%"
                ),
                "Weight (%)": st.column_config.NumberColumn(
                    "Weight (%)", format="%.2%%"
                ),
            },
            use_container_width=True,
            hide_index=True,
        )

    st.caption(
        f"Snapshot date: {snap['snapshot_date']}  |  "
        f"Source: {snap['source']}  |  "
        f"NAV: ${nav:,.2f}"
    )

# ── Realized P&L Summary ───────────────────────────────────────────────────

st.subheader("Realized P&L (YTD)")

try:
    from datetime import date
    ytd_start = date(date.today().year, 1, 1)
    pnl_df = realized_pnl_summary(engine, strategy_id, start=ytd_start)

    if pnl_df.empty:
        st.info("No realized P&L data available yet.")
    else:
        display_pnl = pnl_df.copy()
        display_pnl.columns = ["Ticker", "Total P&L", "Fills", "Wash Sale"]
        display_pnl["Wash Sale"] = display_pnl["Wash Sale"].apply(
            lambda x: "Yes" if x else "No"
        )

        st.dataframe(
            display_pnl,
            column_config={
                "Total P&L": st.column_config.NumberColumn(
                    "Total P&L", format="$%.2f"
                ),
            },
            use_container_width=True,
            hide_index=True,
        )

        total_pnl = float(pnl_df["total_pnl"].sum())
        st.metric("Total Realized P&L (YTD)", f"${total_pnl:,.2f}")
except (sa_exc.SQLAlchemyError, OSError) as exc:
    st.caption(f"Realized P&L unavailable: {type(exc).__name__}")
