"""Page 7 — Audit Trail.

Immutable record of all fills, decisions, and compliance events.
Read-only; no interactive mutations (C3 enforced).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import exc as sa_exc, text

from reporting.dashboards.components.circuit_breaker import (
    render_circuit_breaker_sidebar,
    render_circuit_breaker_warning,
)
from reporting.dashboards.components.env_banner import render_env_banner
from reporting.dashboards.db import get_engine
from reporting.dashboards.queries import (
    active_strategy_id,
    blotter_approval_history,
    fill_history,
    realized_pnl_summary,
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
        selected_fill = st.selectbox(
            "Select a fill to view lineage",
            fill_ids,
            format_func=lambda fid: (
                f"{fid[:12]}... — "
                f"{fills_df.loc[fills_df['fill_id'] == fid, 'ticker'].iloc[0]} "
                f"{fills_df.loc[fills_df['fill_id'] == fid, 'side'].iloc[0]}"
                if len(fills_df.loc[fills_df['fill_id'] == fid]) > 0
                else str(fid)
            ),
        )

        if selected_fill:
            fill_row = fills_df[fills_df["fill_id"] == selected_fill].iloc[0]

            with st.expander("Fill Details", expanded=True):
                detail_cols = st.columns(3)
                detail_cols[0].markdown(f"**Ticker:** {fill_row['ticker']}")
                detail_cols[0].markdown(f"**Side:** {fill_row['side']}")
                detail_cols[0].markdown(f"**Quantity:** {fill_row['filled_quantity']}")
                detail_cols[1].markdown(f"**Avg Fill Price:** ${float(fill_row['avg_fill_price']):.4f}")
                if fill_row.get("realized_pnl") is not None:
                    detail_cols[1].markdown(f"**Realized P&L:** ${float(fill_row['realized_pnl']):.2f}")
                if fill_row.get("cost_basis_per_share") is not None:
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
                with engine.connect() as conn:
                    alpha_row = conn.execute(
                        text("""
                            SELECT alpha_score, rank, universe_size, score_date
                            FROM alpha_scores
                            WHERE ticker = :ticker AND strategy_id = :sid
                              AND score_date <= :fdate
                            ORDER BY score_date DESC
                            LIMIT 1
                        """),
                        {"ticker": fill_ticker, "sid": fill_sid, "fdate": fill_date},
                    ).mappings().fetchone()

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
                with engine.connect() as conn:
                    factor_rows = pd.read_sql_query(
                        text("""
                            SELECT factor_name, z_score, raw_value, score_date
                            FROM factor_scores
                            WHERE ticker = :ticker AND strategy_id = :sid
                              AND score_date <= :fdate
                              AND score_date = (
                                  SELECT MAX(score_date) FROM factor_scores
                                  WHERE ticker = :ticker AND strategy_id = :sid
                                    AND score_date <= :fdate
                              )
                            ORDER BY factor_name ASC
                        """),
                        conn,
                        params={"ticker": fill_ticker, "sid": fill_sid, "fdate": fill_date},
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
    with engine.connect() as conn:
        wash_df = pd.read_sql_query(
            text("""
                SELECT fill_id, fill_timestamp, ticker, filled_quantity,
                       avg_fill_price, realized_pnl, wash_sale_disallowed
                FROM trade_fills
                WHERE side = 'SELL'
                  AND (wash_sale_disallowed = 1 OR realized_pnl < 0)
                ORDER BY fill_timestamp DESC
            """),
            conn,
        )

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
            display_approvals["n_selected"] = display_approvals["selected_order_ids"].apply(
                lambda x: len(json.loads(x)) if isinstance(x, str) else (len(x) if isinstance(x, list) else 0)
            )

        st.dataframe(
            display_approvals,
            use_container_width=True,
            hide_index=True,
        )
except (sa_exc.SQLAlchemyError, OSError):
    st.caption("Blotter approval history unavailable.")
