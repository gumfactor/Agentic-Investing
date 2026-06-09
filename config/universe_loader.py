"""Load and filter the eligible trading universe.

Reads config/universe.yaml and returns a list of ticker symbols that
pass all eligibility filters.

Phase 1 implementation uses S&P 500 constituents from Wikipedia as a free,
no-key-required proxy universe. This introduces minor survivorship bias
(Wikipedia reflects current membership, not historical). Phase 2 replaces
this with point-in-time constituent history from Polygon.io.

The survivorship bias caveat is documented here deliberately so it is not
forgotten. All backtests run during Phase 1 should be interpreted with this
limitation in mind.
"""

from __future__ import annotations

import io
import os
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import structlog
import yaml

logger = structlog.get_logger(__name__)

_CONFIG_PATH = Path(__file__).parent / "universe.yaml"


def load_universe(config_path: Optional[Path] = None) -> list[str]:
    """Return a list of eligible ticker symbols per the universe config.

    This is a best-effort list — no guarantee of point-in-time correctness
    in Phase 1 (Wikipedia reflects current S&P 500 membership).

    Args:
        config_path: Override for the universe config YAML path.

    Returns:
        Sorted list of uppercase ticker strings.
    """
    cfg = _load_config(config_path or _CONFIG_PATH)
    source = cfg.get("source", "sp500_wikipedia")

    if source == "sp500_wikipedia":
        tickers = _fetch_sp500_from_wikipedia()
    elif source == "csv":
        csv_path = cfg.get("csv_path")
        if not csv_path:
            raise ValueError("universe.yaml: source=csv requires csv_path to be set")
        tickers = _load_from_csv(Path(csv_path))
    else:
        raise ValueError(f"Unsupported universe source: {source!r}")

    # Apply force overrides
    force_exclude = set(cfg.get("force_exclude", []))
    force_include = list(cfg.get("force_include", []))

    tickers = [t for t in tickers if t not in force_exclude]
    for t in force_include:
        if t not in tickers:
            tickers.append(t)

    tickers = sorted(set(tickers))
    logger.info("universe_loaded", source=source, count=len(tickers))
    return tickers


# ─── Source implementations ───────────────────────────────────────────────────

def _fetch_sp500_from_wikipedia() -> list[str]:
    """Fetch current S&P 500 constituents from Wikipedia.

    Uses requests with a browser User-Agent (Wikipedia returns 403 to the
    default Python/urllib agent) then passes the HTML to pandas.read_html.
    Falls back to an empty list if Wikipedia is unreachable so the pipeline
    degrades gracefully rather than crashing.

    PHASE 1 CAVEAT: This is current membership, not historical. Use only for
    development and rough backtesting. Replace with Polygon constituent history
    for production-quality backtests.
    """
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        # pandas 2.x requires io.StringIO for inline HTML strings;
        # passing a raw string is treated as a file path.
        tables = pd.read_html(io.StringIO(response.text))
        # First table on the page has the current constituents
        df = tables[0]
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        logger.info("sp500_fetched_from_wikipedia", count=len(tickers))
        return tickers
    except Exception as exc:
        logger.error(
            "sp500_wikipedia_fetch_failed",
            error=str(exc),
            fallback="returning empty universe",
        )
        return []


def _load_from_csv(csv_path: Path) -> list[str]:
    df = pd.read_csv(csv_path)
    if "ticker" not in df.columns:
        raise ValueError(f"CSV at {csv_path} must have a 'ticker' column")
    return df["ticker"].str.upper().dropna().tolist()


def _load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)
