"""Shared helpers for OHLC-based factor modules.

Expects a single long-format DataFrame with columns:
    date, ticker, open, high, low, close
"""
from __future__ import annotations
import pandas as pd


_REQUIRED_OHLC_COLS = {"date", "ticker", "open", "high", "low", "close"}
_REQUIRED_OHLCV_COLS = _REQUIRED_OHLC_COLS | {"volume"}


def validate_ohlc(ohlc: pd.DataFrame) -> None:
    missing = _REQUIRED_OHLC_COLS - set(ohlc.columns)
    if missing:
        raise ValueError(f"ohlc DataFrame missing required columns: {missing}")
    if ohlc.empty:
        raise ValueError("ohlc DataFrame is empty")


def validate_ohlcv(ohlcv: pd.DataFrame) -> None:
    missing = _REQUIRED_OHLCV_COLS - set(ohlcv.columns)
    if missing:
        raise ValueError(f"ohlcv DataFrame missing required columns: {missing}")
    if ohlcv.empty:
        raise ValueError("ohlcv DataFrame is empty")


def ohlc_wide(ohlc: pd.DataFrame, col: str) -> pd.DataFrame:
    """Pivot a single OHLC column to wide format (index=date, columns=ticker)."""
    wide = (
        ohlc[["date", "ticker", col]]
        .assign(**{col: lambda df: df[col].astype(float)})
        .pivot_table(index="date", columns="ticker", values=col)
        .sort_index()
    )
    wide.columns.name = None
    return wide
