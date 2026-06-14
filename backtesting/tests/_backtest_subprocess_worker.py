"""Subprocess worker for the cross-process reproducibility test.

Runs a fully deterministic backtest with synthetic data and writes a JSON
result file.  Intentionally does NOT pin PYTHONHASHSEED — the caller sets it
to different values across three invocations to prove the engine's output is
identical regardless of Python's hash-randomisation of dict/set iteration.

Critical: synthetic prices must NOT use hash() — Python's built-in hash is
seed-dependent.  All data generation here is purely arithmetic.

Usage (internal):
    PYTHONHASHSEED=<n> python backtesting/tests/_backtest_subprocess_worker.py <out.json>
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import pandas as pd

from backtesting.engine.data_handler import DataHandler
from backtesting.engine.event_loop import BacktestEngine
from backtesting.engine.fill_simulator import FillSimulator

# Deterministic per-ticker price offsets — no hash(), no randomness.
_TICKER_OFFSET = {"AAPL": 50.0, "GOOG": 30.0, "MSFT": 10.0, "AMZN": 40.0, "META": 20.0}
_TICKERS = sorted(_TICKER_OFFSET)           # alphabetical → stable order


def _make_prices() -> pd.DataFrame:
    start = date(2023, 1, 2)
    dates = [start + timedelta(days=i) for i in range(90) if (start + timedelta(days=i)).weekday() < 5]
    return pd.DataFrame(
        {"ticker": t, "date": d, "close": 100.0 + _TICKER_OFFSET[t]}
        for d in dates
        for t in _TICKERS
    )


def _make_signals() -> pd.DataFrame:
    signal_start = date(2022, 12, 28)
    dates = [signal_start + timedelta(days=i) for i in range(0, 90, 21)
             if (signal_start + timedelta(days=i)).weekday() < 5]
    return pd.DataFrame(
        {"ticker": t, "score_date": d, "alpha_score": float(j + i * 0.1)}
        for i, d in enumerate(dates)
        for j, t in enumerate(_TICKERS)
    )


def _make_benchmark() -> pd.DataFrame:
    start = date(2023, 1, 2)
    dates = [start + timedelta(days=i) for i in range(90) if (start + timedelta(days=i)).weekday() < 5]
    return pd.DataFrame({"date": dates, "close": [400.0 * (1 + 0.001 * i) for i in range(len(dates))]})


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <output.json>", file=sys.stderr)
        sys.exit(1)

    out_path = sys.argv[1]

    handler = DataHandler(_make_prices(), _make_signals(), _make_benchmark())

    config = {
        "name": "reproducibility_test",
        "version": 1,
        "data_version": "snapshot-v1",
        "portfolio": {
            "method": "equal_weight",
            "n_long": 3,
            "rebalance_frequency": "monthly",
        },
        "backtest": {
            "start_date": "2023-01-02",
            "end_date": "2023-03-31",
            "initial_capital": 100_000.0,
            "benchmark": "SPY",
        },
        "execution": {"fill_model": "perfect"},
    }

    result = BacktestEngine().run(config, handler, FillSimulator(fill_model="perfect"))

    output = {
        "nav_series": result.nav_series.tolist(),
        "nav_index": [str(d) for d in result.nav_series.index],
        "returns": result.returns.tolist(),
        "config_hash": result.config_hash,
        "n_trades": len(result.trades),
        "trade_tickers": result.trades["ticker"].tolist() if not result.trades.empty else [],
        "trade_directions": result.trades["direction"].tolist() if not result.trades.empty else [],
    }

    with open(out_path, "w") as fh:
        json.dump(output, fh)


if __name__ == "__main__":
    main()
