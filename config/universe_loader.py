"""Load and filter the eligible trading universe — OPERATIONAL CURRENT MODE ONLY.

Reads config/universe.yaml and returns a list of ticker symbols that
pass all eligibility filters.

This module reflects *current* S&P 500 membership. It is the explicit
operational current-universe mode kept only for non-historical ingestion
(daily price fetch for the paper pipeline). It MUST NOT be used for
historical research: IC validation, score backfills, and backtests require
``data.universe.runtime.load_universe_as_of`` / ``PITUniverseLookup``
(BUG-008), which fail closed without validated point-in-time membership.
``data.universe.runtime.load_current_universe`` wraps this module in a
``CurrentUniverseSnapshot`` type that historical code rejects at the type
level via ``require_historical_universe``.

Fail-closed note (01B-2): a Wikipedia fetch failure now raises
``UniverseFetchError`` rather than returning an empty universe. The
pre-01B-2 behavior of returning ``[]`` on failure silently emptied
downstream pipelines.
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


class UniverseFetchError(Exception):
    """The current-universe source could not be fetched. Fail closed."""


def load_universe(config_path: Optional[Path] = None) -> list[str]:
    """Return CURRENT eligible ticker symbols per the universe config.

    OPERATIONAL CURRENT MODE ONLY — reflects current S&P 500 membership with
    no point-in-time guarantee. Never use for historical research; see the
    module docstring (BUG-008).

    Args:
        config_path: Override for the universe config YAML path.

    Returns:
        Sorted list of uppercase ticker strings.

    Raises:
        UniverseFetchError: if the source fetch fails (fail closed — an
            empty universe is never silently returned).
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
    Raises UniverseFetchError if Wikipedia is unreachable or unparseable:
    a silently empty universe (the pre-01B-2 behavior) empties every
    downstream pipeline without any alert, which is worse than a loud,
    retryable task failure.

    CAVEAT: this is current membership, not historical. Historical research
    must use data.universe.runtime (BUG-008).
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
    except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
        logger.error("sp500_wikipedia_fetch_failed", error=str(exc))
        raise UniverseFetchError(
            f"Failed to fetch current S&P 500 constituents from Wikipedia: {exc}. "
            "Failing closed rather than returning an empty universe (BUG-008/01B-2)."
        ) from exc


def _load_from_csv(csv_path: Path) -> list[str]:
    df = pd.read_csv(csv_path)
    if "ticker" not in df.columns:
        raise ValueError(f"CSV at {csv_path} must have a 'ticker' column")
    return df["ticker"].str.upper().dropna().tolist()


def _load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)
