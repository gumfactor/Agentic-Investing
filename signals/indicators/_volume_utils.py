"""Shared helpers for volume-based factor modules."""
from __future__ import annotations
import pandas as pd


def validate_volumes(volumes: pd.DataFrame) -> None:
    required = {"date", "ticker", "volume"}
    missing = required - set(volumes.columns)
    if missing:
        raise ValueError(f"volumes DataFrame missing required columns: {missing}")
    if volumes.empty:
        raise ValueError("volumes DataFrame is empty")


def vol_to_wide(volumes: pd.DataFrame) -> pd.DataFrame:
    """Pivot long-format (ticker, date, volume) to wide (index=date, columns=ticker)."""
    wide = (
        volumes[["ticker", "date", "volume"]]
        .assign(volume=lambda df: df["volume"].astype(float))
        .pivot_table(index="date", columns="ticker", values="volume")
        .sort_index()
    )
    wide.columns.name = None
    return wide
