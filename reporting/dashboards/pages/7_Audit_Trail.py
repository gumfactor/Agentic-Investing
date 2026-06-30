"""Page 7 — Audit Trail.

Immutable record of all fills, decisions, and compliance events.
Read-only; no interactive mutations (C3 enforced).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

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
    alpha_score_at_fill_date,
    blotter_approval_history,
    factor_scores_at_fill_date,
    fill_history,
    wash_sale_history,
)

st.set_page_config(page_title="Audit Trail — RQIS", page_icon="📋", layout="wide")

render_env_banner()
render_circuit_breaker_warning()
render_circuit_breaker_sidebar()

st.title("Audit Trail")

engine = get_engine()

# ── Strategy selector ───────────────────────────────────────────────────────

try:
    active_sid = active_strategy_id(engine)
except (sa_exc.SQLAlchemyError, OSError):
    active_sid = None

strategy_id = st.sidebar.text_input(
    "Strategy ID", value=active_sid or "v1_base_momentum"
)

# ── Section A: Fill History ────────────────────────────────────────────────

st.subheader("Fill History")

col_f1, col_f2, col_f3, col_f4 = st.columns(4)
with col_f1:
    filter_ticker = st.text_input("Ticker filter", value="", key="fill_ticker")
with col_f2:
    filter_start = st.date_input(
        "Start date",
        value=date.today() - timedelta(days=90),
        key="fill_start",
    )
with col_f3:
    filter_end = st.date_input("End date", value=date.today(), key="fill_end")
with col_f4:
    filter_side = st.selectbox("Side", ["All", "BUY", "SELL"], key="fill_side")

try:
    fills_df = fill_history(
        engine,
        strategy_id=strategy_id,
        ticker=filter_ticker.strip().upper() if filter_ticker.strip() else None,
        start=filter_start,
        end=filter_end,
        side=filter_side if filter_side != "All" else None,
    )
except (sa_exc.SQLAlchemyError, OSError):
    fills_df = pd.DataFrame()

if fills_df.empty:
    st.info("No fills found for the selected filters.")
else:
    display_fills = fills_df.copy()
    st.dataframe(
        display_fills,
        column_config={
            "avg_fill_price": st.column_config.NumberColumn("Avg Fill Price", format="$%.4f"),
            "realized_pnl": st.column_config.NumberColumn("Realized P&L", format="$%.2f"),
            "cost_basis_per_share": st.column_config.NumberColumn("Cost Basis", format="$%.4f"),
        },
        use_container_width=True,
        hide_index=True,
    )

    st.caption(f"Showing {len(fills_df)} fill(s).")

    # ── Section B: P&L Lineage (drill-down) ────────────────────────────────

    st.subheader("P&L Lineage")

    fill_ids = fills_df["fill_id"].tolist()
    if fill_ids:
        fill_labels = {
            row["fill_id"]: f"{row['fill_id'][:12]}... — {row['ticker']} {row['side']}"
            for _, row in fills_df.iterrows()
        }
        selected_fill = st.selectbox(
            "Select a fill to view lineage",
            fill_ids,
            format_func=lambda fid: fill_labels.get(fid, str(fid)),
        )

        if selected_fill:
            fill_row = fills_df[fills_df["fill_id"] == selected_fill].iloc[0]

            with st.expander("Fill Details", expanded=True):
                detail_cols = st.columns(3)
                detail_cols[0].markdown(f"**Ticker:** {fill_row['ticker']}")
                detail_cols[0].markdown(f"**Side:** {fill_row['side']}")
                detail_cols[0].markdown(f"**Quantity:** {fill_row['filled_quantity']}")
                detail_cols[1].markdown(f"**Avg Fill Price:** ${float(fill_row['avg_fill_price']):.4f}")
                if pd.notna(fill_row.get("realized_pnl")):
                    detail_cols[1].markdown(f"**Realized P&L:** ${float(fill_row['realized_pnl']):.2f}")
                if pd.notna(fill_row.get("cost_basis_per_share")):
                    detail_cols[1].markdown(f"**Cost Basis:** ${float(fill_row['cost_basis_per_share']):.4f}")
                detail_cols[2].markdown(f"**Fill Time:** {fill_row['fill_timestamp']}")
                detail_cols[2].markdown(f"**Strategy:** {fill_row.get('strategy_id', 'N/A')}")
                wash = fill_row.get("wash_sale_disallowed")
                if wash:
                    detail_cols[2].markdown("**Wash Sale:** Yes")

            # Alpha score on fill date
            fill_date = fill_row["fill_timestamp"]
            fill_ticker = fill_row["ticker"]
            fill_sid = fill_row.get("strategy_id", strategy_id)

            try:
                alpha_row = alpha_score_at_fill_date(
                    engine, fill_ticker, fill_sid, fill_date
                )
                if alpha_row:
                    with st.expander("Alpha Score at Fill Time"):
                        acols = st.columns(4)
                        acols[0].metric("Alpha Score", f"{float(alpha_row['alpha_score']):.4f}")
                        acols[1].metric("Rank", int(alpha_row["rank"]))
                        acols[2].metric("Universe Size", int(alpha_row["universe_size"]))
                        acols[3].metric("Score Date", str(alpha_row["score_date"]))
            except (sa_exc.SQLAlchemyError, OSError):
                pass

            # Factor scores on fill date
            try:
                factor_rows = factor_scores_at_fill_date(
                    engine, fill_ticker, fill_sid, fill_date
                )
                if not factor_rows.empty:
                    with st.expander("Factor Scores at Fill Time"):
                        st.dataframe(
                            factor_rows,
                            column_config={
                                "z_score": st.column_config.NumberColumn("Z-Score", format="%.3f"),
                                "raw_value": st.column_config.NumberColumn("Raw Value", format="%.4f"),
                            },
                            use_container_width=True,
                            hide_index=True,
                        )
            except (sa_exc.SQLAlchemyError, OSError):
                pass

# ── Section C: Wash-Sale History ──────────────────────────────────────────

st.divider()
st.subheader("Wash-Sale History")

try:
    wash_df = wash_sale_history(engine)

    if wash_df.empty:
        st.info("No wash-sale or loss fills recorded.")
    else:
        st.dataframe(
            wash_df,
            column_config={
                "avg_fill_price": st.column_config.NumberColumn("Avg Fill Price", format="$%.4f"),
                "realized_pnl": st.column_config.NumberColumn("Realized P&L", format="$%.2f"),
            },
            use_container_width=True,
            hide_index=True,
        )
except (sa_exc.SQLAlchemyError, OSError):
    st.caption("Wash-sale history unavailable.")

# ── Section D: Blotter Approval History ───────────────────────────────────

st.divider()
st.subheader("Blotter Approval History")

try:
    approvals_df = blotter_approval_history(engine, limit=100)

    if approvals_df.empty:
        st.info("No blotter approvals recorded.")
    else:
        display_approvals = approvals_df.copy()
        if "confirmed_blotter_sha256" in display_approvals.columns:
            display_approvals["confirmed_blotter_sha256"] = (
                display_approvals["confirmed_blotter_sha256"].apply(
                    lambda h: h[:16] + "..." if isinstance(h, str) and len(h) > 16 else h
                )
            )
        if "selected_order_ids" in display_approvals.columns:
            def _count_selected(x):
                if not x:
                    return 0
                if isinstance(x, list):
                    return len(x)
                if isinstance(x, str):
                    try:
                        return len(json.loads(x))
                    except (json.JSONDecodeError, TypeError):
                        return 0
                return 0

            display_approvals["n_selected"] = display_approvals["selected_order_ids"].apply(_count_selected)

        st.dataframe(
            display_approvals,
            use_container_width=True,
            hide_index=True,
        )
except (sa_exc.SQLAlchemyError, OSError):
    st.caption("Blotter approval history unavailable.")
