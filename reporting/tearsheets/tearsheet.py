"""TearsheetGenerator — assembles performance charts into HTML or PNG outputs.

Usage (from a BacktestResult)::

    from reporting.tearsheets import TearsheetGenerator

    gen = TearsheetGenerator.from_backtest_result(result, title="v1 Base Momentum")
    gen.render_html(Path("output/tearsheet.html"))
    gen.render_png_dir(Path("output/charts/"))

For paper/live trading, construct directly::

    gen = TearsheetGenerator(
        returns=daily_returns,
        benchmark_returns=spy_returns,
        positions=positions_df,
        trades=trades_df,
        metrics={},
        config=strategy_config,
        initial_capital=1_000_000.0,
        title="Paper Run 2026-06-20",
    )
"""
from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd

from reporting.tearsheets import charts
from reporting.tearsheets.metrics import compute_metrics

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fig_to_b64(fig: plt.Figure, dpi: int = 150) -> str:
    """Encode a matplotlib Figure as a base64 PNG string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _pct(value: float, decimals: int = 1) -> str:
    if math.isnan(value):
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value * 100:.{decimals}f}%"


def _num(value: float, decimals: int = 2) -> str:
    if math.isnan(value):
        return "—"
    return f"{value:.{decimals}f}"


def _int_str(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _dollar(value: float) -> str:
    if math.isnan(value):
        return "—"
    return f"${value:,.0f}"


def _bps(value: float) -> str:
    if math.isnan(value):
        return "—"
    return f"{value:.1f} bps"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class TearsheetGenerator:
    """Holds all data required to render a performance tearsheet.

    All inputs use the same conventions as BacktestResult:
    - returns / benchmark_returns: daily decimal returns, index = date objects
    - positions: DataFrame, index = date, columns = ticker, values = weight
    - trades: DataFrame with columns date/ticker/direction/shares/fill_price/
              notional/commission/market_impact/total_cost
    """
    returns: pd.Series
    benchmark_returns: pd.Series
    positions: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict
    config: dict
    initial_capital: float = 1_000_000.0
    title: str = ""
    prices: Optional[pd.DataFrame] = None  # for entry/exit charts (long-format)
    nav_series: Optional[pd.Series] = None  # if available, used for equity curve

    # Lazily computed full metrics
    _full_metrics: dict = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def from_backtest_result(
        cls,
        result: object,
        prices: Optional[pd.DataFrame] = None,
        title: str = "",
    ) -> "TearsheetGenerator":
        """Build from a ``BacktestResult`` dataclass."""
        config = getattr(result, "config", {})
        initial_capital = config.get("backtest", {}).get("initial_capital", 1_000_000.0)
        if not title:
            title = config.get("name", "Tearsheet")
        return cls(
            returns=result.returns,
            benchmark_returns=result.benchmark_returns,
            positions=result.positions,
            trades=result.trades,
            metrics=result.metrics,
            config=config,
            initial_capital=float(initial_capital),
            title=title,
            prices=prices,
            nav_series=result.nav_series,
        )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def full_metrics(self) -> dict:
        """Compute (or return cached) full metrics dictionary."""
        if not self._full_metrics:
            self._full_metrics = compute_metrics(
                self.returns,
                self.benchmark_returns,
                self.trades,
                self.initial_capital,
                base_metrics=self.metrics,
            )
        return self._full_metrics

    def _date_range(self) -> str:
        if self.returns.empty:
            return "—"
        start = pd.to_datetime(self.returns.index[0]).strftime("%Y-%m-%d")
        end = pd.to_datetime(self.returns.index[-1]).strftime("%Y-%m-%d")
        return f"{start} → {end}"

    # ------------------------------------------------------------------
    # Chart generation
    # ------------------------------------------------------------------

    def _build_charts(self) -> dict[str, plt.Figure]:
        """Generate all chart figures with Agg backend (non-interactive)."""
        matplotlib.use("Agg")
        m = self.full_metrics()
        ic = self.initial_capital

        fig_map: dict[str, plt.Figure] = {}

        # 1. Equity curve
        fig_map["equity_curve"] = charts.equity_curve(
            self.returns, self.benchmark_returns,
            title="Cumulative Return (indexed to 100)"
        )

        # 2. Drawdown
        fig_map["drawdown"] = charts.drawdown(self.returns)

        # 3. Monthly heatmap
        fig_map["monthly_returns"] = charts.monthly_returns_heatmap(
            self.returns, figsize=(12, max(3.0, len(self.returns) / 252 * 0.6 + 2))
        )

        # 4. Rolling Sharpe
        fig_map["rolling_sharpe"] = charts.rolling_sharpe_chart(self.returns)

        # 5. Annual returns
        fig_map["annual_returns"] = charts.annual_returns_bar(
            self.returns, self.benchmark_returns
        )

        # 6. Return distribution
        fig_map["return_distribution"] = charts.return_distribution(
            self.returns, self.benchmark_returns
        )

        # 7. Position concentration
        if not self.positions.empty:
            fig_map["position_concentration"] = charts.position_concentration(
                self.positions
            )

        # 8. Cumulative costs
        fig_map["cumulative_costs"] = charts.cumulative_costs(
            self.trades, ic
        )

        # 9. Entry/exit for the most-traded ticker (when prices available)
        if self.prices is not None and not self.trades.empty:
            top_ticker = (
                self.trades.groupby("ticker")["notional"].sum().idxmax()
                if "notional" in self.trades.columns
                else self.trades["ticker"].value_counts().idxmax()
            )
            fig_map["trade_entry_exit"] = charts.trade_entry_exit(
                top_ticker, self.prices, self.trades
            )

        return fig_map

    # ------------------------------------------------------------------
    # Render: HTML
    # ------------------------------------------------------------------

    def render_html(self, output_path: str | Path) -> Path:
        """Render a self-contained HTML tearsheet with embedded charts.

        Returns the path to the written file.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        fig_map = self._build_charts()
        b64 = {name: _fig_to_b64(fig) for name, fig in fig_map.items()}
        m = self.full_metrics()

        html = _build_html(
            title=self.title or "Performance Tearsheet",
            date_range=self._date_range(),
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            config=self.config,
            metrics=m,
            b64_charts=b64,
        )

        output_path.write_text(html, encoding="utf-8")
        return output_path

    # ------------------------------------------------------------------
    # Render: PNG directory
    # ------------------------------------------------------------------

    def render_png_dir(self, output_dir: str | Path) -> list[Path]:
        """Save each chart as a separate PNG into ``output_dir``.

        Returns list of paths written.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        fig_map = self._build_charts()
        paths: list[Path] = []
        for name, fig in fig_map.items():
            dest = output_dir / f"{name}.png"
            fig.savefig(dest, dpi=150, bbox_inches="tight",
                        facecolor="white", edgecolor="none")
            plt.close(fig)
            paths.append(dest)

        return paths


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

_CHART_TITLES = {
    "equity_curve":          "Equity Curve",
    "drawdown":              "Drawdown",
    "monthly_returns":       "Monthly Returns",
    "rolling_sharpe":        "Rolling Sharpe",
    "annual_returns":        "Annual Returns",
    "return_distribution":   "Return Distribution",
    "position_concentration":"Position Concentration",
    "cumulative_costs":      "Transaction Costs",
    "trade_entry_exit":      "Trade Entry / Exit",
}


def _metric_rows(m: dict) -> str:
    """Build the HTML for the metrics summary grid."""

    def card(label: str, value: str, sub: str = "") -> str:
        sub_html = f'<div class="metric-sub">{sub}</div>' if sub else ""
        return (
            f'<div class="metric-card">'
            f'<div class="metric-label">{label}</div>'
            f'<div class="metric-value">{value}</div>'
            f'{sub_html}'
            f'</div>'
        )

    nan = float("nan")
    cards = [
        card("Total Return",     _pct(m.get("total_return", nan)),
             f'Benchmark: {_pct(m.get("benchmark_total_return", nan))}'),
        card("CAGR",             _pct(m.get("cagr", nan))),
        card("Ann. Volatility",  _pct(m.get("annualized_volatility", nan), 2)),
        card("Sharpe Ratio",     _num(m.get("sharpe", nan))),
        card("Sortino Ratio",    _num(m.get("sortino", nan))),
        card("Max Drawdown",     _pct(m.get("max_drawdown", nan))),
        card("Calmar Ratio",     _num(m.get("calmar", nan))),
        card("Info. Ratio",      _num(m.get("information_ratio", nan))),
        card("Beta",             _num(m.get("beta", nan))),
        card("Alpha",            _pct(m.get("alpha", nan))),
        card("Best Month",       _pct(m.get("best_month", nan))),
        card("Worst Month",      _pct(m.get("worst_month", nan))),
        card("% Positive Months",_pct(m.get("positive_months_pct", nan))),
        card("# Trades",         _int_str(m.get("n_trades", nan))),
        card("Total Tx Cost",    _dollar(m.get("total_transaction_cost", nan))),
        card("Avg Cost",         _bps(m.get("avg_trade_cost_bps", nan))),
    ]
    return "\n".join(cards)


def _build_html(
    title: str,
    date_range: str,
    generated_at: str,
    config: dict,
    metrics: dict,
    b64_charts: dict[str, str],
) -> str:
    strategy_name = config.get("name", title)
    data_version  = config.get("data_version", "—")
    benchmark     = config.get("backtest", {}).get("benchmark", "—")

    chart_sections = "\n".join(
        f'<div class="chart-block">'
        f'<h3 class="chart-title">{_CHART_TITLES.get(name, name)}</h3>'
        f'<img src="data:image/png;base64,{b64}" class="chart-img" alt="{name}" />'
        f'</div>'
        for name, b64 in b64_charts.items()
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title} — Tearsheet</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
    background: #f8fafc;
    color: #1e293b;
    padding: 24px;
  }}
  .header {{
    background: #1e3a5f;
    color: white;
    padding: 20px 28px;
    border-radius: 8px;
    margin-bottom: 20px;
  }}
  .header h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .header .meta {{ font-size: 11px; opacity: 0.75; display: flex; gap: 24px; flex-wrap: wrap; }}
  .metrics-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 10px;
    margin-bottom: 24px;
  }}
  .metric-card {{
    background: white;
    border-radius: 6px;
    padding: 10px 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,.08);
  }}
  .metric-label {{ font-size: 10px; color: #64748b; text-transform: uppercase;
                   letter-spacing: .4px; margin-bottom: 4px; }}
  .metric-value {{ font-size: 16px; font-weight: 700; color: #1e293b; }}
  .metric-sub   {{ font-size: 10px; color: #94a3b8; margin-top: 2px; }}
  .charts-section {{ display: flex; flex-direction: column; gap: 20px; }}
  .chart-block {{
    background: white;
    border-radius: 6px;
    padding: 12px 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,.08);
  }}
  .chart-title {{ font-size: 13px; font-weight: 600; color: #475569;
                  margin-bottom: 8px; }}
  .chart-img {{ width: 100%; height: auto; display: block; }}
  .footer {{ margin-top: 24px; text-align: center; font-size: 10px; color: #94a3b8; }}
</style>
</head>
<body>

<div class="header">
  <h1>{title}</h1>
  <div class="meta">
    <span>Strategy: {strategy_name}</span>
    <span>Period: {date_range}</span>
    <span>Benchmark: {benchmark}</span>
    <span>Data: {data_version}</span>
    <span>Generated: {generated_at}</span>
  </div>
</div>

<div class="metrics-grid">
{_metric_rows(metrics)}
</div>

<div class="charts-section">
{chart_sections}
</div>

<div class="footer">
  RQIS Performance Tearsheet &mdash; {generated_at}
</div>

</body>
</html>
"""
