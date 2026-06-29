"""Page 6 — Signals.

Alpha score leaderboard, factor breakdown, factor history, and strategy registry.
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
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
    all_strategies,
    factor_scores_for_ticker,
    latest_alpha_scores,
)

st.set_page_config(page_title="Signals — RQIS", page_icon="📡", layout="wide")

render_env_banner()
render_circuit_breaker_warning()
render_circuit_breaker_sidebar()

st.title("Signals")

engine = get_engine()

# ── Strategy selector ───────────────────────────────────────────────────────

try:
    active_sid = active_strategy_id(engine)
except (sa_exc.SQLAlchemyError, OSError):
    active_sid = None

strategy_id = st.sidebar.text_input(
    "Strategy ID", value=active_sid or "v1_base_momentum"
)

# ── Section A: Alpha Score Leaderboard ──────────────────────────────────────

st.subheader("Alpha Score Leaderboard")

try:
    scores_df = latest_alpha_scores(engine, strategy_id, limit=100)
except (sa_exc.SQLAlchemyError, OSError):
    scores_df = pd.DataFrame()

if scores_df.empty:
    st.info("No alpha scores available for this strategy.")
else:
    universe_size = int(scores_df["universe_size"].iloc[0]) if "universe_size" in scores_df.columns else len(scores_df)

    # Top 25
    top = scores_df.head(25)
    # Bottom 25
    bottom = scores_df.tail(25) if len(scores_df) > 25 else pd.DataFrame()

    col_top, col_bottom = st.columns(2)

    with col_top:
        st.markdown("**Top 25**")
        st.dataframe(
            top[["ticker", "alpha_score", "rank"]],
            column_config={
                "alpha_score": st.column_config.NumberColumn("Alpha Score", format="%.4f"),
                "rank": st.column_config.NumberColumn("Rank"),
            },
            use_container_width=True,
            hide_index=True,
        )

    with col_bottom:
        if not bottom.empty:
            st.markdown("**Bottom 25**")
            st.dataframe(
                bottom[["ticker", "alpha_score", "rank"]],
                column_config={
                    "alpha_score": st.column_config.NumberColumn("Alpha Score", format="%.4f"),
                    "rank": st.column_config.NumberColumn("Rank"),
                },
                use_container_width=True,
                hide_index=True,
            )

    st.caption(f"Universe size: {universe_size}  |  Strategy: {strategy_id}")

    # ── Section B: Factor Breakdown (per ticker) ────────────────────────────

    st.subheader("Factor Breakdown")

    all_tickers = scores_df["ticker"].tolist()
    selected_ticker = st.selectbox("Select ticker", all_tickers, index=0)

    if selected_ticker:
        try:
            factors_df = factor_scores_for_ticker(
                engine, selected_ticker, strategy_id, lookback_days=1
            )
        except (sa_exc.SQLAlchemyError, OSError):
            factors_df = pd.DataFrame()

        if factors_df.empty:
            st.info(f"No factor scores available for {selected_ticker}.")
        else:
            # Bar chart of z-scores
            factors_df = factors_df.sort_values("z_score", ascending=True)
            fig, ax = plt.subplots(figsize=(8, max(3, len(factors_df) * 0.4)))
            colors = ["#dc3545" if z < 0 else "#198754" for z in factors_df["z_score"]]
            ax.barh(factors_df["factor_name"], factors_df["z_score"].astype(float), color=colors)
            ax.set_xlabel("Z-Score")
            ax.set_title(f"Factor Z-Scores — {selected_ticker}")
            ax.axvline(x=0, color="gray", linewidth=0.5)
            fig.tight_layout()
            st.pyplot(fig)
            plt.close(fig)

            st.dataframe(
                factors_df[["factor_name", "z_score", "raw_value"]],
                column_config={
                    "z_score": st.column_config.NumberColumn("Z-Score", format="%.3f"),
                    "raw_value": st.column_config.NumberColumn("Raw Value", format="%.4f"),
                },
                use_container_width=True,
                hide_index=True,
            )

        # ── Section C: Factor Score History (30 days) ───────────────────────

        st.subheader("Factor Score History (30 days)")

        try:
            history_df = factor_scores_for_ticker(
                engine, selected_ticker, strategy_id, lookback_days=30
            )
        except (sa_exc.SQLAlchemyError, OSError):
            history_df = pd.DataFrame()

        if history_df.empty:
            st.info(f"No factor history available for {selected_ticker}.")
        else:
            history_df["score_date"] = pd.to_datetime(history_df["score_date"])
            factor_names = history_df["factor_name"].unique()

            fig, ax = plt.subplots(figsize=(10, 4))
            for fname in factor_names:
                fdata = history_df[history_df["factor_name"] == fname]
                ax.plot(fdata["score_date"], fdata["z_score"].astype(float), label=fname)
            ax.legend(fontsize=7, loc="upper left")
            ax.set_ylabel("Z-Score")
            ax.set_title(f"Factor Z-Score History — {selected_ticker}")
            ax.axhline(y=0, color="gray", linewidth=0.5)
            fig.tight_layout()
            st.pyplot(fig)
            plt.close(fig)

# ── Section D: Strategy Registry ────────────────────────────────────────────

st.divider()
st.subheader("Strategy Registry")

try:
    strats_df = all_strategies(engine)
except (sa_exc.SQLAlchemyError, OSError):
    strats_df = pd.DataFrame()

if strats_df.empty:
    st.info("No strategies registered.")
else:
    display_df = strats_df.copy()
    display_df.columns = ["Strategy ID", "Status", "Created"]

    st.dataframe(
        display_df,
        column_config={
            "Status": st.column_config.TextColumn("Status"),
        },
        use_container_width=True,
        hide_index=True,
    )

# ── Section E: Cross-Strategy Alpha Overlap ───────────────────────────────

st.divider()
st.subheader("Cross-Strategy Alpha Overlap")

if not strats_df.empty and len(strats_df) >= 2:
    overlap_sids = strats_df["strategy_id"].tolist()
    top_n = st.slider("Top N positions for overlap", min_value=5, max_value=50, value=20)

    try:
        from reporting.dashboards.simulation import alpha_overlap_matrix

        overlap_df = alpha_overlap_matrix(engine, overlap_sids, top_n=top_n)

        if not overlap_df.empty:
            import numpy as np

            fig, ax = plt.subplots(figsize=(max(6, len(overlap_sids) * 1.2), max(5, len(overlap_sids))))
            im = ax.imshow(overlap_df.values, cmap="YlOrRd", vmin=0, vmax=1, aspect="auto")
            ax.set_xticks(range(len(overlap_sids)))
            ax.set_yticks(range(len(overlap_sids)))
            ax.set_xticklabels(overlap_sids, rotation=45, ha="right", fontsize=8)
            ax.set_yticklabels(overlap_sids, fontsize=8)

            for i in range(len(overlap_sids)):
                for j in range(len(overlap_sids)):
                    val = overlap_df.values[i, j]
                    ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                            color="white" if val > 0.5 else "black", fontsize=8)

            fig.colorbar(im, ax=ax, label="Jaccard Similarity")
            ax.set_title(f"Alpha Overlap (Top {top_n} Positions)")
            fig.tight_layout()
            st.pyplot(fig)
            plt.close(fig)

            st.caption(
                "High overlap (>0.7) suggests strategies are not diversifying well. "
                "Low overlap (<0.3) indicates complementary signal sources."
            )
    except (sa_exc.SQLAlchemyError, OSError, ValueError) as exc:
        st.caption(f"Alpha overlap unavailable: {type(exc).__name__}")
else:
    st.info("At least two strategies are needed for overlap analysis.")
