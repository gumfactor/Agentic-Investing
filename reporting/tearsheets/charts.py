"""Matplotlib chart builders for tearsheets.

Each public function either:
  • creates a new Figure (when ``ax`` is None) and returns it, or
  • draws on the caller-supplied Axes and returns None.

This dual API lets charts be used standalone (for HTML embedding as base64 PNGs)
or packed into a composite GridSpec figure (for PDF / PNG exports).

Backend note: this module does not force a backend.  Call
``matplotlib.use("Agg")`` before importing if you need non-interactive rendering,
or use ``plt.switch_backend("Agg")`` at the call site.
"""
from __future__ import annotations

from typing import Optional

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns
from scipy.stats import norm as scipy_norm

from reporting.tearsheets.metrics import (
    _MONTH_LABELS,
    _to_dt,
    annual_returns,
    drawdown_series,
    monthly_returns_pivot,
    rolling_sharpe,
)

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

_C = {
    "strategy":  "#2563EB",  # blue
    "benchmark": "#64748B",  # slate
    "positive":  "#16A34A",  # green
    "negative":  "#DC2626",  # red
    "cost":      "#EA580C",  # orange
    "neutral":   "#94A3B8",  # light slate
}

_SPINE_STYLE = {
    "axes.spines.top":   False,
    "axes.spines.right": False,
    "axes.grid":         True,
    "grid.alpha":        0.25,
    "figure.facecolor":  "white",
    "axes.facecolor":    "white",
}


def _style() -> None:
    plt.rcParams.update(_SPINE_STYLE)


def _new_fig(figsize: tuple[float, float]) -> tuple[plt.Figure, plt.Axes]:
    _style()
    fig, ax = plt.subplots(figsize=figsize)
    return fig, ax


def _maybe_return(fig: Optional[plt.Figure], ax: Optional[plt.Axes]) -> Optional[plt.Figure]:
    """Tighten layout and return fig only when we created it (ax was None)."""
    if fig is not None:
        fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# 1. Equity curve
# ---------------------------------------------------------------------------

def equity_curve(
    returns: pd.Series,
    benchmark_returns: pd.Series,
    nav_series: Optional[pd.Series] = None,
    ax: Optional[plt.Axes] = None,
    figsize: tuple[float, float] = (10, 4),
    title: str = "Cumulative Return (indexed to 100)",
) -> Optional[plt.Figure]:
    """Strategy vs benchmark cumulative return.

    When ``nav_series`` is supplied (e.g. from BacktestResult), the strategy
    curve is derived from it directly (indexed to 100 at start), giving an
    accurate picture including the very first trading day's NAV.  Falls back
    to ``(1 + returns).cumprod()`` when nav_series is None.

    The benchmark is inner-aligned to the strategy date range so that missing
    benchmark dates never inject spurious 0%-return days.
    """
    fig: Optional[plt.Figure] = None
    if ax is None:
        fig, ax = _new_fig(figsize)
    else:
        _style()

    # Guard: nothing to plot if both returns and nav_series are empty
    if returns.empty and (nav_series is None or nav_series.empty):
        ax.text(0.5, 0.5, "No return data", ha="center", va="center",
                transform=ax.transAxes)
        ax.set_title(title, fontsize=10, fontweight="bold")
        return _maybe_return(fig, ax)

    # Strategy cumulative curve
    if nav_series is not None and not nav_series.empty:
        first_val = float(nav_series.iloc[0])
        strat_cum = (100.0 / first_val) * nav_series if first_val != 0 else 100.0 * (1 + returns).cumprod()
    else:
        cum = 100.0 * (1 + returns).cumprod()
        # Prepend a synthetic day-0 anchor so the curve originates at exactly 100
        idx0 = pd.Timestamp(cum.index[0]) - pd.Timedelta(days=1)
        strat_cum = pd.concat([pd.Series([100.0], index=[idx0]), cum])

    # Benchmark: inner-align, then normalise to 100 at the first shared date so
    # both curves share the same inception level regardless of the strategy path.
    bm_r, _ = benchmark_returns.align(returns, join="inner")
    if not bm_r.empty:
        bm_cum_inner = 100.0 * (1 + bm_r).cumprod()
        bm_cum_inner = bm_cum_inner / bm_cum_inner.iloc[0] * 100.0
    else:
        bm_cum_inner = pd.Series(dtype=float)

    dates = pd.to_datetime(strat_cum.index)
    ax.plot(dates, strat_cum.values, color=_C["strategy"], lw=1.5, label="Strategy")
    if not bm_cum_inner.empty:
        bm_dates = pd.to_datetime(bm_cum_inner.index)
        ax.plot(bm_dates, bm_cum_inner.values, color=_C["benchmark"], lw=1.2,
                ls="--", label="Benchmark")
    ax.axhline(100, color=_C["neutral"], lw=0.7, ls=":")

    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_ylabel("Indexed value")
    ax.legend(loc="upper left", fontsize=8)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}"))

    return _maybe_return(fig, ax)


# ---------------------------------------------------------------------------
# 2. Drawdown
# ---------------------------------------------------------------------------

def drawdown(
    returns: pd.Series,
    ax: Optional[plt.Axes] = None,
    figsize: tuple[float, float] = (10, 3),
    title: str = "Drawdown",
) -> Optional[plt.Figure]:
    fig: Optional[plt.Figure] = None
    if ax is None:
        fig, ax = _new_fig(figsize)
    else:
        _style()

    dd_pct = drawdown_series(returns) * 100.0
    dates = pd.to_datetime(dd_pct.index)

    ax.fill_between(dates, dd_pct.values, 0.0, color=_C["negative"], alpha=0.45)
    ax.plot(dates, dd_pct.values, color=_C["negative"], lw=0.8)
    ax.axhline(0, color="black", lw=0.6)

    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_ylabel("Drawdown (%)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.1f}%"))

    return _maybe_return(fig, ax)


# ---------------------------------------------------------------------------
# 3. Monthly returns heatmap
# ---------------------------------------------------------------------------

def monthly_returns_heatmap(
    returns: pd.Series,
    ax: Optional[plt.Axes] = None,
    figsize: tuple[float, float] = (12, 4),
    title: str = "Monthly Returns (%)",
) -> Optional[plt.Figure]:
    fig: Optional[plt.Figure] = None
    if ax is None:
        fig, ax = _new_fig(figsize)
    else:
        _style()

    pivot = monthly_returns_pivot(returns) * 100.0

    if pivot.empty:
        ax.text(0.5, 0.5, "Insufficient data for monthly heatmap",
                ha="center", va="center", transform=ax.transAxes)
        ax.set_title(title, fontsize=10, fontweight="bold")
        return _maybe_return(fig, ax)

    # Ensure all 12 month columns appear
    for label in _MONTH_LABELS:
        if label not in pivot.columns:
            pivot[label] = np.nan
    pivot = pivot[_MONTH_LABELS]

    # Append YTD column (use integer year as index to align with pivot)
    s_dt = _to_dt(returns)
    ytd = (1 + s_dt).resample("YE").prod() - 1
    ytd.index = ytd.index.year.astype(int)
    pivot["YTD"] = ytd * 100.0

    # Auto-scale colour range
    vals = pivot.values[~np.isnan(pivot.values)]
    vmax = max(float(np.abs(vals).max()) if len(vals) else 5.0, 5.0)

    sns.heatmap(
        pivot,
        ax=ax,
        annot=True,
        fmt=".1f",
        center=0.0,
        cmap="RdYlGn",
        vmin=-vmax,
        vmax=vmax,
        linewidths=0.4,
        linecolor="white",
        cbar_kws={"shrink": 0.7, "label": "%"},
        annot_kws={"size": 7.5},
    )
    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.tick_params(axis="x", rotation=0, labelsize=8)
    ax.tick_params(axis="y", rotation=0, labelsize=8)

    return _maybe_return(fig, ax)


# ---------------------------------------------------------------------------
# 4. Rolling Sharpe
# ---------------------------------------------------------------------------

def rolling_sharpe_chart(
    returns: pd.Series,
    window: int = 252,
    ax: Optional[plt.Axes] = None,
    figsize: tuple[float, float] = (10, 3),
    title: str = "Rolling 252-Day Sharpe Ratio",
) -> Optional[plt.Figure]:
    fig: Optional[plt.Figure] = None
    if ax is None:
        fig, ax = _new_fig(figsize)
    else:
        _style()

    rs = rolling_sharpe(returns, window)
    dates = pd.to_datetime(rs.index)
    valid = ~np.isnan(rs.values)

    if valid.any():
        ax.fill_between(dates[valid], rs.values[valid], 0.0,
                        where=(rs.values[valid] >= 0),
                        color=_C["positive"], alpha=0.25)
        ax.fill_between(dates[valid], rs.values[valid], 0.0,
                        where=(rs.values[valid] < 0),
                        color=_C["negative"], alpha=0.25)
        ax.plot(dates[valid], rs.values[valid], color=_C["strategy"], lw=1.2)

    ax.axhline(0, color="black", lw=0.7)
    ax.axhline(1, color=_C["positive"], lw=0.8, ls="--", alpha=0.55, label="Sharpe = 1")
    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_ylabel("Sharpe")
    ax.legend(fontsize=7)

    return _maybe_return(fig, ax)


# ---------------------------------------------------------------------------
# 5. Annual returns bar
# ---------------------------------------------------------------------------

def annual_returns_bar(
    returns: pd.Series,
    benchmark_returns: pd.Series,
    ax: Optional[plt.Axes] = None,
    figsize: tuple[float, float] = (8, 4),
    title: str = "Annual Returns",
) -> Optional[plt.Figure]:
    fig: Optional[plt.Figure] = None
    if ax is None:
        fig, ax = _new_fig(figsize)
    else:
        _style()

    ann = annual_returns(returns) * 100.0
    bm_r, _ = benchmark_returns.align(returns, join="inner")
    bm_ann = annual_returns(bm_r) * 100.0

    strat_by_year = {int(dt.year): float(v) for dt, v in zip(pd.to_datetime(ann.index), ann.values)}
    bm_by_year = {int(dt.year): float(v) for dt, v in zip(pd.to_datetime(bm_ann.index), bm_ann.values)}
    years = sorted(set(strat_by_year) | set(bm_by_year))

    if not years:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        ax.set_title(title, fontsize=10, fontweight="bold")
        return _maybe_return(fig, ax)

    # Detect partial years (first/last years that don't span the full calendar year)
    s_dt = _to_dt(returns)
    first_date = s_dt.index[0]
    last_date = s_dt.index[-1]
    first_year = int(first_date.year)
    last_year = int(last_date.year)
    has_partial = False

    def _is_partial(y: int) -> bool:
        if y == first_year and first_date.month > 1:
            return True
        if y == last_year and last_date.month < 12:
            return True
        return False

    year_labels = []
    for y in years:
        if _is_partial(y):
            year_labels.append(f"{y}*")
            has_partial = True
        else:
            year_labels.append(str(y))

    x = np.arange(len(years))
    w = 0.38
    strat_vals = [strat_by_year.get(y, np.nan) for y in years]
    bm_vals = [bm_by_year.get(y, np.nan) for y in years]

    bar_colors = [_C["positive"] if (v is not None and not np.isnan(v) and v >= 0)
                  else _C["negative"] for v in strat_vals]
    ax.bar(x - w / 2, strat_vals, w, color=bar_colors, label="Strategy", alpha=0.85)
    ax.bar(x + w / 2, bm_vals, w, color=_C["benchmark"], label="Benchmark", alpha=0.7)

    ax.axhline(0, color="black", lw=0.7)
    ax.set_xticks(x)
    ax.set_xticklabels(year_labels, rotation=45, ha="right", fontsize=8)
    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_ylabel("Return (%)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.legend(fontsize=8)
    if has_partial:
        ax.text(0.01, 0.02, "* Partial year", transform=ax.transAxes,
                fontsize=7, color=_C["neutral"], va="bottom")

    return _maybe_return(fig, ax)


# ---------------------------------------------------------------------------
# 6. Return distribution
# ---------------------------------------------------------------------------

def return_distribution(
    returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    ax: Optional[plt.Axes] = None,
    figsize: tuple[float, float] = (8, 4),
    title: str = "Daily Return Distribution",
) -> Optional[plt.Figure]:
    fig: Optional[plt.Figure] = None
    if ax is None:
        fig, ax = _new_fig(figsize)
    else:
        _style()

    r_pct = returns * 100.0
    ax.hist(r_pct.dropna(), bins=50, color=_C["strategy"], alpha=0.55,
            label="Strategy", density=True)

    if benchmark_returns is not None:
        bm_pct = benchmark_returns.reindex(returns.index).dropna() * 100.0
        if not bm_pct.empty:
            ax.hist(bm_pct, bins=50, color=_C["benchmark"], alpha=0.35,
                    label="Benchmark", density=True)

    # Normal-distribution overlay
    mu, sigma = float(r_pct.mean()), float(r_pct.std(ddof=1))
    if sigma > 0:
        x_range = np.linspace(float(r_pct.min()), float(r_pct.max()), 300)
        ax.plot(x_range, scipy_norm.pdf(x_range, mu, sigma),
                color=_C["strategy"], lw=1.5, ls="--", label="Normal fit")

    ax.axvline(0, color="black", lw=0.7)
    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_xlabel("Daily Return (%)")
    ax.set_ylabel("Density")
    ax.legend(fontsize=8)

    return _maybe_return(fig, ax)


# ---------------------------------------------------------------------------
# 7. Position concentration
# ---------------------------------------------------------------------------

def position_concentration(
    positions: pd.DataFrame,
    top_n: int = 10,
    ax: Optional[plt.Axes] = None,
    figsize: tuple[float, float] = (10, 4),
    title: str = "Top Position Weights Over Time",
) -> Optional[plt.Figure]:
    """Stacked area of the top-N tickers by average weight."""
    fig: Optional[plt.Figure] = None
    if ax is None:
        fig, ax = _new_fig(figsize)
    else:
        _style()

    if positions.empty:
        ax.text(0.5, 0.5, "No position data", ha="center", va="center",
                transform=ax.transAxes)
        ax.set_title(title, fontsize=10, fontweight="bold")
        return _maybe_return(fig, ax)

    avg = positions.mean()
    top_tickers = avg.nlargest(min(top_n, len(avg))).index.tolist()
    pos_top = positions[top_tickers].fillna(0.0)

    dates = pd.to_datetime(pos_top.index)
    cmap = plt.colormaps.get_cmap("tab20")
    colors = [cmap(i / max(len(top_tickers), 1)) for i in range(len(top_tickers))]

    ax.stackplot(dates, [pos_top[t].values for t in top_tickers],
                 labels=top_tickers, colors=colors, alpha=0.8)

    # Distinguish non-top-N holdings ("Other") from genuine unallocated cash
    total_all = positions.fillna(0.0).sum(axis=1)
    total_top = pos_top.sum(axis=1)
    other_frac = (total_all - total_top).clip(0.0, 1.0)
    cash_frac = (1.0 - total_all).clip(0.0, 1.0)
    if (other_frac > 0.01).any():
        ax.stackplot(dates, [other_frac.values], labels=["Other"],
                     colors=[_C["benchmark"]], alpha=0.4)
    if (cash_frac > 0.01).any():
        ax.stackplot(dates, [cash_frac.values], labels=["Cash"],
                     colors=[_C["neutral"]], alpha=0.4)

    ax.set_ylim(0.0, 1.05)
    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_ylabel("Portfolio Weight")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.legend(loc="upper left", fontsize=6, ncol=2,
               bbox_to_anchor=(1.01, 1.0), borderaxespad=0)

    return _maybe_return(fig, ax)


# ---------------------------------------------------------------------------
# 8. Cumulative transaction costs
# ---------------------------------------------------------------------------

def cumulative_costs(
    trades: pd.DataFrame,
    initial_capital: float = 1_000_000.0,
    ax: Optional[plt.Axes] = None,
    figsize: tuple[float, float] = (10, 3),
    title: str = "Cumulative Transaction Costs (% of Initial Capital)",
) -> Optional[plt.Figure]:
    fig: Optional[plt.Figure] = None
    if ax is None:
        fig, ax = _new_fig(figsize)
    else:
        _style()

    if trades.empty or "total_cost" not in trades.columns:
        ax.text(0.5, 0.5, "No transaction cost data", ha="center", va="center",
                transform=ax.transAxes)
        ax.set_title(title, fontsize=10, fontweight="bold")
        return _maybe_return(fig, ax)

    df = trades.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    pct = 100.0 / max(initial_capital, 1.0)
    cum_total = df.groupby("date")["total_cost"].sum().cumsum() * pct

    ax.fill_between(cum_total.index, cum_total.values, color=_C["cost"], alpha=0.35)
    ax.plot(cum_total.index, cum_total.values, color=_C["cost"], lw=1.3,
            label="Total cost")

    if "commission" in df.columns and "market_impact" in df.columns:
        cum_comm = df.groupby("date")["commission"].sum().cumsum() * pct
        cum_mi = df.groupby("date")["market_impact"].sum().cumsum() * pct
        ax.plot(cum_comm.index, cum_comm.values, color=_C["negative"],
                lw=1.0, ls="--", label="Commission")
        ax.plot(cum_mi.index, cum_mi.values, color=_C["cost"],
                lw=1.0, ls=":", label="Market impact")
        ax.legend(fontsize=7)

    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_ylabel("% of initial capital")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.2f}%"))

    return _maybe_return(fig, ax)


# ---------------------------------------------------------------------------
# 9. Trade entry/exit overlay
# ---------------------------------------------------------------------------

def trade_entry_exit(
    ticker: str,
    prices: pd.DataFrame,
    trades: pd.DataFrame,
    ax: Optional[plt.Axes] = None,
    figsize: tuple[float, float] = (12, 4),
    title: Optional[str] = None,
) -> Optional[plt.Figure]:
    """Price chart with BUY (▲) and SELL (▼) markers.

    ``prices`` can be either:
    • long-format: columns ``ticker``, ``date``, ``close``
    • wide-format: index = date, column per ticker
    """
    if title is None:
        title = f"{ticker} — Trade Entry / Exit"

    fig: Optional[plt.Figure] = None
    if ax is None:
        fig, ax = _new_fig(figsize)
    else:
        _style()

    # Resolve price series
    if "ticker" in prices.columns:
        sub = prices[prices["ticker"] == ticker]
        if sub.empty:
            ax.text(0.5, 0.5, f"No price data for {ticker}", ha="center", va="center",
                    transform=ax.transAxes)
            ax.set_title(title, fontsize=10, fontweight="bold")
            return _maybe_return(fig, ax)
        p = sub.set_index("date")["close"]
    elif ticker in prices.columns:
        p = prices[ticker].dropna()
    else:
        ax.text(0.5, 0.5, f"No price data for {ticker}", ha="center", va="center",
                transform=ax.transAxes)
        ax.set_title(title, fontsize=10, fontweight="bold")
        return _maybe_return(fig, ax)

    p.index = pd.to_datetime(p.index)
    ax.plot(p.index, p.values, color=_C["neutral"], lw=1.0, zorder=1, label="Price")

    if not trades.empty and "direction" in trades.columns:
        t = trades[trades["ticker"] == ticker].copy()
        t["date"] = pd.to_datetime(t["date"])
        buys = t[t["direction"] == "BUY"]
        sells = t[t["direction"] == "SELL"]
        if not buys.empty:
            ax.scatter(buys["date"], buys["fill_price"],
                       marker="^", color=_C["positive"], s=70, zorder=3,
                       label=f"BUY ({len(buys)})", alpha=0.9)
        if not sells.empty:
            ax.scatter(sells["date"], sells["fill_price"],
                       marker="v", color=_C["negative"], s=70, zorder=3,
                       label=f"SELL ({len(sells)})", alpha=0.9)

    ax.set_title(title, fontsize=10, fontweight="bold")
    ax.set_ylabel("Price (USD)")
    ax.legend(fontsize=8)

    return _maybe_return(fig, ax)
