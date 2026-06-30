"""Page 5 — Performance.

Strategy performance over time: metrics cards, tearsheet charts,
and strategy comparison panel.
"""
from __future__ import annotations

from datetime import date, timedelta

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
    daily_returns_for_tickers,
    nav_history,
    strategy_simulations_query,
)

st.set_page_config(page_title="Performance — RQIS", page_icon="📈", layout="wide")

render_env_banner()
render_circuit_breaker_warning()
render_circuit_breaker_sidebar()

st.title("Performance")

engine = get_engine()

# ── Strategy selector ───────────────────────────────────────────────────────

try:
    active_sid = active_strategy_id(engine)
except (sa_exc.SQLAlchemyError, OSError):
    active_sid = None

strategy_id = st.sidebar.text_input(
    "Strategy ID", value=active_sid or "v1_base_momentum"
)

# Date range
range_options = {"YTD": None, "3M": 90, "6M": 180, "1Y": 365, "All": 3650}
selected_range = st.sidebar.radio("Date Range", list(range_options.keys()), index=0)

if selected_range == "YTD":
    start_date = date(date.today().year, 1, 1)
else:
    lookback = range_options[selected_range]
    start_date = date.today() - timedelta(days=lookback)

# ── Section A: Live strategy metrics ────────────────────────────────────────

st.subheader("Strategy Metrics")

try:
    nav_df = nav_history(engine, strategy_id, lookback_days=3650)
except (sa_exc.SQLAlchemyError, OSError):
    nav_df = pd.DataFrame()

if nav_df.empty:
    st.info("No NAV history available for this strategy.")
else:
    nav_df["snapshot_date"] = pd.to_datetime(nav_df["snapshot_date"])
    nav_df = nav_df.set_index("snapshot_date").sort_index()
    nav_df["nav_usd"] = nav_df["nav_usd"].astype(float)

    # Filter to date range
    mask = nav_df.index >= pd.Timestamp(start_date)
    nav_filtered = nav_df[mask]

    if len(nav_filtered) < 2:
        st.warning("Insufficient data for selected date range.")
    else:
        returns = nav_filtered["nav_usd"].pct_change().dropna()

        # Compute metrics
        try:
            from reporting.tearsheets.metrics import (
                annualized_return,
                annualized_volatility,
                calmar_ratio,
                max_drawdown,
                sharpe_ratio,
                sortino_ratio,
            )

            total_ret = float((1 + returns).prod() - 1)
            cagr = annualized_return(returns)
            vol = annualized_volatility(returns)
            sharpe = sharpe_ratio(returns)
            sortino = sortino_ratio(returns)
            mdd = max_drawdown(returns)
            calmar = calmar_ratio(returns)

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Return", f"{total_ret:.2%}")
            col2.metric("CAGR", f"{cagr:.2%}")
            col3.metric("Sharpe Ratio", f"{sharpe:.2f}")
            col4.metric("Sortino Ratio", f"{sortino:.2f}")

            col5, col6, col7 = st.columns(3)
            col5.metric("Max Drawdown", f"{mdd:.2%}")
            col6.metric("Calmar Ratio", f"{calmar:.2f}")
            col7.metric("Volatility", f"{vol:.2%}")

        except (ImportError, ValueError, ZeroDivisionError, TypeError, KeyError) as exc:
            st.warning(f"Metrics computation failed: {type(exc).__name__}: {exc}")

        # Section B: Charts
        st.subheader("Charts")

        # Fetch benchmark returns
        try:
            bench_df = daily_returns_for_tickers(engine, ["SPY"], lookback_days=3650)
            if not bench_df.empty and "SPY" in bench_df.columns:
                bench_rets = bench_df["SPY"].dropna()
            else:
                bench_rets = pd.Series(dtype=float)
        except (sa_exc.SQLAlchemyError, OSError):
            bench_rets = pd.Series(dtype=float)

        try:
            from reporting.tearsheets.charts import (
                annual_returns_bar,
                drawdown as drawdown_chart,
                equity_curve,
                monthly_returns_heatmap,
                rolling_sharpe_chart,
            )

            # Equity curve
            fig = equity_curve(returns, bench_rets)
            if fig:
                st.pyplot(fig)
                plt.close(fig)

            # Drawdown
            fig = drawdown_chart(returns)
            if fig:
                st.pyplot(fig)
                plt.close(fig)

            # Monthly returns heatmap
            fig = monthly_returns_heatmap(returns)
            if fig:
                st.pyplot(fig)
                plt.close(fig)

            # Rolling Sharpe
            fig = rolling_sharpe_chart(returns)
            if fig:
                st.pyplot(fig)
                plt.close(fig)

            # Annual returns
            fig = annual_returns_bar(returns)
            if fig:
                st.pyplot(fig)
                plt.close(fig)

        except (ImportError, ValueError, TypeError, KeyError) as exc:
            st.warning(f"Chart rendering failed: {type(exc).__name__}: {exc}")

# ── Section C: Strategy Comparison ──────────────────────────────────────────

st.divider()
st.subheader("Strategy Comparison")

try:
    strats_df = all_strategies(engine)
except (sa_exc.SQLAlchemyError, OSError) as exc:
    st.error(f"Strategy registry query failed: {type(exc).__name__}")
    strats_df = pd.DataFrame()

if strats_df.empty:
    st.info("No strategies found in the registry.")
else:
    available_sids = strats_df["strategy_id"].tolist()
    selected_sids = st.multiselect(
        "Select strategies to compare",
        available_sids,
        default=available_sids[:3] if len(available_sids) >= 3 else available_sids,
    )

    if selected_sids:
        try:
            sim_df = strategy_simulations_query(
                engine, selected_sids, start=start_date
            )
        except (sa_exc.SQLAlchemyError, OSError):
            sim_df = pd.DataFrame()

        if sim_df.empty:
            st.info("No simulation data available for selected strategies.")
        else:
            # Build comparison table
            comparison_rows = []
            for sid in selected_sids:
                sid_data = sim_df[sim_df["strategy_id"] == sid]
                if sid_data.empty:
                    continue
                rets = sid_data["simulated_return"].astype(float)
                total_ret = float((1 + rets).prod() - 1)

                try:
                    from reporting.tearsheets.metrics import (
                        annualized_return,
                        max_drawdown,
                        sharpe_ratio,
                    )
                    sharpe_val = sharpe_ratio(rets)
                    mdd_val = max_drawdown(rets)
                except Exception:
                    sharpe_val = None
                    mdd_val = None

                status = strats_df.loc[
                    strats_df["strategy_id"] == sid, "status"
                ].values
                status_str = status[0] if len(status) > 0 else "unknown"

                comparison_rows.append({
                    "Strategy": sid,
                    "Status": status_str,
                    "Return": total_ret,
                    "Sharpe": sharpe_val,
                    "Max DD": mdd_val,
                    "Days": len(sid_data),
                })

            if comparison_rows:
                comp_df = pd.DataFrame(comparison_rows)
                st.dataframe(
                    comp_df,
                    column_config={
                        "Return": st.column_config.NumberColumn("Return", format="%.2f%%"),
                        "Max DD": st.column_config.NumberColumn("Max DD", format="%.2f%%"),
                        "Sharpe": st.column_config.NumberColumn("Sharpe", format="%.2f"),
                    },
                    use_container_width=True,
                    hide_index=True,
                )

                # Equity curve overlay
                fig = None
                try:
                    fig, ax = plt.subplots(figsize=(10, 5))
                    for sid in selected_sids:
                        sid_data = sim_df[sim_df["strategy_id"] == sid].sort_values("sim_date")
                        if sid_data.empty:
                            continue
                        nav_vals = sid_data["simulated_nav"].astype(float)
                        first_nav = float(nav_vals.iloc[0])
                        if first_nav > 0:
                            indexed = 100.0 * nav_vals / first_nav
                            ax.plot(
                                pd.to_datetime(sid_data["sim_date"]),
                                indexed.values,
                                label=sid,
                            )
                    ax.set_title("Strategy Comparison (indexed to 100)")
                    ax.legend()
                    ax.set_ylabel("NAV (indexed)")
                    st.pyplot(fig)
                except (ValueError, TypeError, KeyError) as exc:
                    st.caption(f"Comparison chart unavailable: {type(exc).__name__}")
                finally:
                    if fig is not None:
                        plt.close(fig)
