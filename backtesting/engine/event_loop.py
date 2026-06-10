"""Event-driven backtesting engine.

Architecture
------------
For each simulation date in chronological order:

  1. data_handler.get_close(date)          → current prices
  2. Mark portfolio to market; record daily NAV
  3. On rebalance dates:
     a. data_handler.get_latest_signals()  → alpha_scores (PIT-safe)
     b. select_portfolio()                 → target weights
     c. compute_orders()                   → weight-delta orders
     d. fill_simulator.simulate_fills()    → fills
     e. portfolio.apply_fills()            → update positions/cash
  4. At end of simulation: compute_metrics()

The engine never calls datetime.now().  All dates come from the simulation
clock (the sequence of trading dates from DataHandler).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
import structlog

from backtesting.engine.data_handler import DataHandler
from backtesting.engine.fill_simulator import Fill, FillSimulator, Order, compute_orders

logger = structlog.get_logger(__name__)

_RISK_FREE_RATE = 0.0   # simplified; caller can override via config
_TRADING_DAYS_PER_YEAR = 252


@dataclass
class BacktestResult:
    """Output of a single BacktestEngine.run() call."""
    nav_series: pd.Series          # index=date, values=NAV in USD
    returns: pd.Series             # index=date, values=daily return (decimal)
    benchmark_returns: pd.Series   # index=date, values=benchmark daily return
    positions: pd.DataFrame        # index=date, columns=ticker, values=weight
    trades: pd.DataFrame           # one row per Fill
    metrics: dict                  # sharpe, max_dd, cagr, ir, turnover, etc.
    config: dict
    data_version: str
    config_hash: str               # SHA-256 of the serialised config


@dataclass
class _PortfolioState:
    cash: float
    positions: dict[str, float]    # ticker → shares held

    def nav(self, close_prices: dict[str, float]) -> float:
        equity = sum(
            sh * close_prices.get(tk, 0.0)
            for tk, sh in self.positions.items()
        )
        return self.cash + equity

    def weights(self, close_prices: dict[str, float]) -> dict[str, float]:
        total = self.nav(close_prices)
        if total <= 0:
            return {}
        return {
            tk: (sh * close_prices.get(tk, 0.0)) / total
            for tk, sh in self.positions.items()
            if sh > 0
        }

    def apply_fills(self, fills: list[Fill]) -> None:
        for f in fills:
            if f.direction == "BUY":
                self.positions[f.ticker] = self.positions.get(f.ticker, 0.0) + f.shares
                self.cash -= f.notional + f.total_cost
            else:
                self.positions[f.ticker] = self.positions.get(f.ticker, 0.0) - f.shares
                self.cash += f.notional - f.total_cost
            if self.positions.get(f.ticker, 0.0) < 1e-6:
                self.positions.pop(f.ticker, None)


class BacktestEngine:
    """Event-driven backtest engine.

    Usage::

        engine = BacktestEngine()
        result = engine.run(config, data_handler, fill_simulator)
    """

    def run(
        self,
        config: dict,
        data_handler: DataHandler,
        fill_simulator: FillSimulator,
    ) -> BacktestResult:
        """Run a full backtest.

        Args:
            config: Strategy configuration dict (loaded from YAML).
                Must contain backtest.start_date, backtest.end_date,
                backtest.initial_capital, portfolio.n_long, etc.
            data_handler: PIT-safe data source.
            fill_simulator: Transaction cost / fill model.

        Returns:
            BacktestResult with full performance history and metrics.
        """
        bt_cfg = config["backtest"]
        port_cfg = config["portfolio"]

        start = _parse_date(bt_cfg["start_date"])
        end = _parse_date(bt_cfg["end_date"])
        initial_capital = float(bt_cfg["initial_capital"])
        n_long = int(port_cfg["n_long"])
        rebal_freq = port_cfg.get("rebalance_frequency", "monthly")
        data_version = config.get("data_version", "")

        trading_dates = data_handler.trading_dates(start, end)
        if not trading_dates:
            raise ValueError(f"No trading dates found between {start} and {end}")

        rebal_dates = set(data_handler.rebalance_dates(start, end, rebal_freq))
        portfolio = _PortfolioState(cash=initial_capital, positions={})

        nav_records: list[tuple[date, float]] = []
        weight_records: list[tuple[date, dict[str, float]]] = []
        all_fills: list[Fill] = []
        config_hash = _hash_config(config)

        logger.info(
            "backtest_started",
            start=str(start),
            end=str(end),
            n_trading_days=len(trading_dates),
            n_rebal_dates=len(rebal_dates),
            data_version=data_version,
        )

        for sim_date in trading_dates:
            close_prices = data_handler.get_close(sim_date)
            if not close_prices:
                continue

            if sim_date in rebal_dates:
                signals = data_handler.get_latest_signals(sim_date)
                if not signals.empty:
                    target_weights = _select_equal_weight(signals, n_long)
                    current_weights = portfolio.weights(close_prices)
                    orders = compute_orders(target_weights, current_weights)
                    nav = portfolio.nav(close_prices)

                    # Pass 1: execute sells first to free cash.
                    sell_orders = [o for o in orders if o.direction == "SELL"]
                    sell_fills = fill_simulator.simulate_fills(
                        sell_orders, close_prices, sim_date, nav
                    )
                    portfolio.apply_fills(sell_fills)
                    all_fills.extend(sell_fills)

                    # Pass 2: scale buys to 99.5% of available cash so transaction
                    # costs cannot push cash below zero on the initial full deployment.
                    buy_orders = [o for o in orders if o.direction == "BUY"]
                    if buy_orders and portfolio.cash > 0:
                        total_buy_delta = sum(abs(o.delta_weight) for o in buy_orders)
                        max_buy_weight = (portfolio.cash * 0.995) / nav
                        scale = min(1.0, max_buy_weight / total_buy_delta) if total_buy_delta > 0 else 0.0
                        if scale > 0:
                            if scale < 1.0:
                                buy_orders = [
                                    Order(
                                        ticker=o.ticker,
                                        direction=o.direction,
                                        target_weight=o.target_weight * scale,
                                        current_weight=o.current_weight,
                                        delta_weight=o.delta_weight * scale,
                                    )
                                    for o in buy_orders
                                ]
                            buy_fills = fill_simulator.simulate_fills(
                                buy_orders, close_prices, sim_date, nav
                            )
                            portfolio.apply_fills(buy_fills)
                            all_fills.extend(buy_fills)

            current_nav = portfolio.nav(close_prices)
            nav_records.append((sim_date, current_nav))
            weight_records.append((sim_date, portfolio.weights(close_prices)))

        nav_series = pd.Series(
            [v for _, v in nav_records],
            index=[d for d, _ in nav_records],
            name="nav",
        )
        returns = nav_series.pct_change().dropna()
        bm_returns = data_handler.get_benchmark_returns_series(start, end)

        trades_df = _fills_to_df(all_fills)
        positions_df = _weights_to_df(weight_records)

        metrics = _compute_metrics(
            returns, bm_returns, trades_df, initial_capital
        )

        logger.info(
            "backtest_complete",
            sharpe=round(metrics.get("sharpe", float("nan")), 3),
            cagr=round(metrics.get("cagr", float("nan")), 4),
            max_dd=round(metrics.get("max_drawdown", float("nan")), 4),
            n_trades=len(all_fills),
            data_version=data_version,
        )

        return BacktestResult(
            nav_series=nav_series,
            returns=returns,
            benchmark_returns=bm_returns,
            positions=positions_df,
            trades=trades_df,
            metrics=metrics,
            config=config,
            data_version=data_version,
            config_hash=config_hash,
        )


# ------------------------------------------------------------------
# Portfolio construction helpers
# ------------------------------------------------------------------

def _select_equal_weight(
    signals: pd.DataFrame,
    n_long: int,
) -> dict[str, float]:
    """Select top n_long tickers by alpha_score and assign equal weights."""
    top = (
        signals.nlargest(n_long, "alpha_score")
        if len(signals) > n_long
        else signals
    )
    if top.empty:
        return {}
    weight = 1.0 / len(top)
    return dict(zip(top["ticker"], [weight] * len(top)))


# ------------------------------------------------------------------
# Performance metrics
# ------------------------------------------------------------------

def _compute_metrics(
    returns: pd.Series,
    bm_returns: pd.Series,
    trades_df: pd.DataFrame,
    initial_capital: float,
) -> dict:
    if returns.empty:
        return {}

    ann_factor = _TRADING_DAYS_PER_YEAR
    mean_ret = returns.mean()
    std_ret = returns.std(ddof=1)
    sharpe = (mean_ret / std_ret * (ann_factor ** 0.5)) if std_ret > 0 else float("nan")

    cagr = _cagr(returns)
    max_dd = _max_drawdown(returns)

    # Information ratio vs benchmark
    common = returns.index.intersection(bm_returns.index)
    if len(common) > 1:
        active = returns.loc[common] - bm_returns.loc[common]
        ir = (active.mean() / active.std(ddof=1) * (ann_factor ** 0.5)) if active.std() > 0 else float("nan")
    else:
        ir = float("nan")

    # Turnover: sum of absolute weight changes / n_rebal_dates, annualised
    turnover = _compute_turnover(trades_df, initial_capital, returns)

    return {
        "sharpe": sharpe,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "information_ratio": ir,
        "annual_turnover": turnover,
        "n_trading_days": len(returns),
        "total_return": float((1 + returns).prod() - 1),
        "daily_vol": float(std_ret * (ann_factor ** 0.5)),
    }


def _cagr(returns: pd.Series) -> float:
    total = float((1 + returns).prod())
    n_years = len(returns) / _TRADING_DAYS_PER_YEAR
    if n_years <= 0 or total <= 0:
        return float("nan")
    return total ** (1 / n_years) - 1


def _max_drawdown(returns: pd.Series) -> float:
    cumulative = (1 + returns).cumprod()
    rolling_max = cumulative.cummax()
    dd = cumulative / rolling_max - 1
    return float(dd.min())


def _compute_turnover(
    trades_df: pd.DataFrame,
    initial_capital: float,
    returns: pd.Series,
) -> float:
    if trades_df.empty or returns.empty:
        return 0.0
    n_years = len(returns) / _TRADING_DAYS_PER_YEAR
    if n_years <= 0:
        return 0.0
    total_notional = float(trades_df["notional"].sum())
    return (total_notional / initial_capital) / n_years


# ------------------------------------------------------------------
# Serialisation helpers
# ------------------------------------------------------------------

def _fills_to_df(fills: list[Fill]) -> pd.DataFrame:
    if not fills:
        return pd.DataFrame(columns=[
            "date", "ticker", "direction", "shares",
            "fill_price", "notional", "commission",
            "market_impact", "total_cost",
        ])
    rows = [
        {
            "date": f.sim_date,
            "ticker": f.ticker,
            "direction": f.direction,
            "shares": f.shares,
            "fill_price": f.fill_price,
            "notional": f.notional,
            "commission": f.commission,
            "market_impact": f.market_impact,
            "total_cost": f.total_cost,
        }
        for f in fills
    ]
    return pd.DataFrame(rows)


def _weights_to_df(records: list[tuple[date, dict[str, float]]]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    rows = {d: weights for d, weights in records}
    df = pd.DataFrame(rows).T
    df.index.name = "date"
    return df.fillna(0.0)


def _hash_config(config: dict) -> str:
    serialised = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(serialised.encode()).hexdigest()


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
