"""Data quality checks for ingested market data.

Checks run after each ingestion batch and before writing to the database.
Detected anomalies are recorded in data_quality_flags but do not block
the write — exclusion decisions are left to the caller.

Error-severity flags do block signal computation until resolved
(see data/storage/timescale_writer.py: upsert_ohlcv writes flags alongside prices).

Thresholds are conservative defaults; adjust via config/settings.yaml.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# A close that moves more than this many σ vs. recent history is flagged.
PRICE_JUMP_SIGMA_THRESHOLD = 3.0

# If more than this fraction of the expected universe has missing bars, flag it.
COMPLETENESS_MISSING_FRACTION_WARNING = 0.05   # 5%
COMPLETENESS_MISSING_FRACTION_ERROR = 0.10     # 10%


def run_quality_checks(df: pd.DataFrame) -> pd.DataFrame:
    """Run all quality checks on a batch of OHLCV bars.

    Args:
        df: DataFrame with columns ticker, date, open, high, low, close, volume.

    Returns:
        DataFrame of flag records with columns:
            ticker, date, flag_type, severity, message
        Empty DataFrame if no issues found.
    """
    if df.empty:
        return pd.DataFrame(columns=["ticker", "date", "flag_type", "severity", "message"])

    flags: list[dict] = []
    flags.extend(_check_negative_prices(df))
    flags.extend(_check_hloc_violations(df))
    flags.extend(_check_zero_volume(df))
    flags.extend(_check_price_jumps(df))

    result = pd.DataFrame(flags, columns=["ticker", "date", "flag_type", "severity", "message"])
    if not result.empty:
        logger.info(
            "quality_check_complete",
            total_flags=len(result),
            errors=int((result["severity"] == "error").sum()),
            warnings=int((result["severity"] == "warning").sum()),
        )
    return result


def check_universe_completeness(
    df: pd.DataFrame,
    expected_tickers: list[str],
    check_date: date,
) -> list[dict]:
    """Check that all expected tickers have a bar for check_date.

    Returns a list of flag dicts (may be empty).
    Used by the daily pipeline to alert on widespread data gaps.
    """
    present = set(df.loc[df["date"] == check_date, "ticker"].unique())
    expected = set(expected_tickers)
    missing = expected - present

    if not missing:
        return []

    fraction_missing = len(missing) / len(expected)
    severity = "error" if fraction_missing >= COMPLETENESS_MISSING_FRACTION_ERROR else "warning"

    flags = []
    for ticker in missing:
        flags.append(
            {
                "ticker": ticker,
                "date": check_date,
                "flag_type": "missing_data",
                "severity": severity,
                "message": (
                    f"No bar for {check_date}. "
                    f"{len(missing)}/{len(expected)} ({fraction_missing:.1%}) tickers missing."
                ),
            }
        )
    return flags


# ─── Individual checks ────────────────────────────────────────────────────────

def _check_negative_prices(df: pd.DataFrame) -> list[dict]:
    flags = []
    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            continue
        # Decimal comparison — convert to float for vectorised ops
        series = df[col].apply(lambda v: float(v) if v is not None else None)
        mask = series.notna() & (series <= 0)
        for _, row in df[mask].iterrows():
            flags.append(
                {
                    "ticker": row["ticker"],
                    "date": row["date"],
                    "flag_type": "negative_price",
                    "severity": "error",
                    "message": f"{col}={row[col]} is zero or negative.",
                }
            )
    return flags


def _check_hloc_violations(df: pd.DataFrame) -> list[dict]:
    """Flag bars where high < low, or close falls outside [low, high]."""
    flags = []
    needed = {"high", "low", "close"}
    if not needed.issubset(df.columns):
        return flags

    df_f = df.copy()
    for col in ["high", "low", "close"]:
        df_f[col + "_f"] = df_f[col].apply(lambda v: float(v) if v is not None else None)

    df_valid = df_f.dropna(subset=["high_f", "low_f", "close_f"])

    hl_violation = df_valid["high_f"] < df_valid["low_f"]
    for _, row in df_valid[hl_violation].iterrows():
        flags.append(
            {
                "ticker": row["ticker"],
                "date": row["date"],
                "flag_type": "hloc_violation",
                "severity": "error",
                "message": f"high ({row['high']}) < low ({row['low']}).",
            }
        )

    close_violation = (df_valid["close_f"] < df_valid["low_f"]) | (df_valid["close_f"] > df_valid["high_f"])
    for _, row in df_valid[close_violation & ~hl_violation].iterrows():
        flags.append(
            {
                "ticker": row["ticker"],
                "date": row["date"],
                "flag_type": "hloc_violation",
                "severity": "warning",
                "message": f"close ({row['close']}) outside [low={row['low']}, high={row['high']}].",
            }
        )
    return flags


def _check_zero_volume(df: pd.DataFrame) -> list[dict]:
    if "volume" not in df.columns:
        return []
    flags = []
    mask = df["volume"].notna() & (df["volume"] == 0)
    for _, row in df[mask].iterrows():
        flags.append(
            {
                "ticker": row["ticker"],
                "date": row["date"],
                "flag_type": "volume_zero",
                "severity": "warning",
                "message": "Volume reported as 0 on a trading day.",
            }
        )
    return flags


def _check_price_jumps(
    df: pd.DataFrame,
    window: int = 20,
    sigma_threshold: float = PRICE_JUMP_SIGMA_THRESHOLD,
) -> list[dict]:
    """Flag closes that deviate >sigma_threshold σ from the recent rolling mean.

    Uses a rolling window on log returns to be scale-invariant.
    Requires at least window+1 bars per ticker to produce a result.
    """
    flags = []

    if "close" not in df.columns:
        return flags

    df_sorted = df.sort_values(["ticker", "date"]).copy()
    df_sorted["close_f"] = df_sorted["close"].apply(lambda v: float(v) if v is not None else None)
    df_sorted = df_sorted.dropna(subset=["close_f"])

    for ticker, group in df_sorted.groupby("ticker"):
        if len(group) < window + 1:
            continue

        closes = group["close_f"].values
        log_returns = np.diff(np.log(closes))

        rolling_mean = pd.Series(log_returns).rolling(window).mean().values
        rolling_std = pd.Series(log_returns).rolling(window).std().values

        for i in range(window, len(log_returns)):
            std = rolling_std[i]
            if std == 0 or np.isnan(std):
                continue
            z = (log_returns[i] - rolling_mean[i]) / std
            if abs(z) > sigma_threshold:
                row = group.iloc[i + 1]  # +1 because log_returns is diff of closes
                flags.append(
                    {
                        "ticker": ticker,
                        "date": row["date"],
                        "flag_type": "price_jump",
                        "severity": "warning",
                        "message": (
                            f"Log return z-score = {z:.2f} (threshold ±{sigma_threshold}). "
                            f"Close = {row['close']}."
                        ),
                    }
                )

    return flags
