"""Cross-check a sample of DB prices against a fresh Yahoo Finance download.

Pulls N random tickers from daily_prices, re-downloads the last 30 trading
days from Yahoo, and compares unadjusted close prices.  Reports mean and max
absolute difference plus any rows that exceed the tolerance.

Usage:
    python scripts/verify_prices.py [--tickers 10] [--days 30] [--tolerance 0.01]

Exits with code 1 if any ticker exceeds the tolerance threshold.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
from datetime import date, timedelta
import os
import random

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import yfinance as yf

load_dotenv()


def _fetch_db_prices(engine, tickers: list[str], start: date, end: date) -> pd.DataFrame:
    query = text("""
        SELECT ticker, date, close::float AS close
        FROM daily_prices
        WHERE ticker = ANY(:tickers)
          AND date BETWEEN :start AND :end
        ORDER BY ticker, date
    """)
    with engine.connect() as conn:
        result = conn.execute(query, {"tickers": tickers, "start": start, "end": end})
        return pd.DataFrame(result.fetchall(), columns=["ticker", "date", "close"])


def _fetch_yahoo_prices(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    raw = yf.download(
        tickers=tickers,
        start=start,
        end=end + timedelta(days=1),
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=True,
    )
    if raw.empty:
        return pd.DataFrame(columns=["ticker", "date", "close"])

    rows = []
    if isinstance(raw.columns, pd.MultiIndex):
        for ticker in tickers:
            try:
                sub = raw.xs(ticker, axis=1, level=1)["Close"]
            except KeyError:
                continue
            for dt, val in sub.dropna().items():
                rows.append({"ticker": ticker, "date": dt.date(), "close": float(val)})
    else:
        for dt, val in raw["Close"].dropna().items():
            rows.append({"ticker": tickers[0], "date": dt.date(), "close": float(val)})

    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-check DB prices vs Yahoo Finance")
    parser.add_argument("--tickers", type=int, default=10, help="Number of random tickers to sample")
    parser.add_argument("--days", type=int, default=30, help="Number of calendar days to check")
    parser.add_argument("--tolerance", type=float, default=0.01, help="Max allowed absolute price difference")
    args = parser.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    end = date.today()
    start = end - timedelta(days=args.days)

    # Sample random tickers from DB
    with engine.connect() as conn:
        all_tickers = [
            r[0] for r in conn.execute(text("SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker"))
        ]
    tickers = random.sample(all_tickers, min(args.tickers, len(all_tickers)))
    print(f"Checking {len(tickers)} tickers over {start} → {end}: {tickers}\n")

    db_df = _fetch_db_prices(engine, tickers, start, end)
    yf_df = _fetch_yahoo_prices(tickers, start, end)

    if yf_df.empty:
        print("ERROR: Yahoo returned no data — rate limited or network issue.")
        sys.exit(1)

    # Merge on ticker + date
    merged = db_df.merge(yf_df, on=["ticker", "date"], suffixes=("_db", "_yf"))
    if merged.empty:
        print("ERROR: No overlapping (ticker, date) rows between DB and Yahoo.")
        sys.exit(1)

    merged["abs_diff"] = (merged["close_db"] - merged["close_yf"]).abs()
    merged["rel_diff_pct"] = merged["abs_diff"] / merged["close_yf"].abs() * 100

    # Summary per ticker
    print(f"{'Ticker':<8}  {'Rows':>5}  {'Mean |Δ|':>10}  {'Max |Δ|':>10}  {'Status'}")
    print("-" * 55)
    any_fail = False
    for ticker, grp in merged.groupby("ticker"):
        mean_diff = grp["abs_diff"].mean()
        max_diff = grp["abs_diff"].max()
        status = "OK" if max_diff <= args.tolerance else "FAIL"
        if status == "FAIL":
            any_fail = True
        print(f"{ticker:<8}  {len(grp):>5}  {mean_diff:>10.4f}  {max_diff:>10.4f}  {status}")

    print()
    print(f"Overall  rows={len(merged)}  mean_abs_diff={merged['abs_diff'].mean():.4f}  "
          f"max_abs_diff={merged['abs_diff'].max():.4f}  tolerance={args.tolerance}")

    # Detail on failures
    failures = merged[merged["abs_diff"] > args.tolerance]
    if not failures.empty:
        print(f"\n{len(failures)} rows exceed tolerance:")
        print(failures[["ticker", "date", "close_db", "close_yf", "abs_diff"]].to_string(index=False))
        sys.exit(1)
    else:
        print("\nAll prices within tolerance. ✓")


if __name__ == "__main__":
    main()
