"""SEC EDGAR fundamentals ingestion client.

Fetches XBRL Company Facts data from the SEC EDGAR API and normalises it
into the financial_statements schema (EAV, with period_end_date and
release_date for point-in-time correctness).

Data flow per ticker
--------------------
1. Resolve ticker → CIK via SEC's company_tickers.json mapping.
2. Fetch companyfacts/CIK{cik}.json — full filing history.
3. For each target XBRL concept (see concept_map.py):
   a. Extract all qualifying observations (annual + quarterly forms only).
   b. Classify period type (annual / quarterly) from observation duration.
   c. Store EVERY observation — including restatements — with the original
      filing date as release_date.  pit_latest() picks the correct value
      at query time.
4. Compute derived items (free_cash_flow = operating_cash_flow − capex).

Point-in-time correctness
--------------------------
Each EDGAR data point carries a `filed` field — the date the company
submitted the document to the SEC.  We store this as release_date.  This
is the literal SEC submission timestamp, the most defensible PIT anchor
available for US public equities.

Rate limiting
-------------
SEC policy: 10 requests/second per IP.  We target 8 req/s (0.13 s delay)
for safety margin.  A User-Agent header with app name + operator email is
required per SEC policy.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import requests
import structlog

from data.ingestion.fundamentals.concept_map import (
    ACCEPTED_FORMS,
    ANNUAL_MAX_DAYS,
    ANNUAL_MIN_DAYS,
    CONCEPT_ALIASES,
    DERIVED_ITEMS,
    QUARTERLY_MAX_DAYS,
    QUARTERLY_MIN_DAYS,
)

logger = structlog.get_logger(__name__)

_BASE_URL = "https://data.sec.gov"
_TICKER_MAP_URL = f"{_BASE_URL}/files/company_tickers.json"
_COMPANY_FACTS_URL = f"{_BASE_URL}/api/xbrl/companyfacts/CIK{{cik}}.json"

# 8 req/s to stay safely under the 10 req/s SEC hard limit.
_INTER_REQUEST_DELAY = 0.13

# Per-request retry delays (seconds).  Covers transient 429/503 responses.
_RETRY_DELAYS = (5, 15, 30)

_SOURCE = "sec_edgar"
_SOURCE_VERSION = "xbrl_companyfacts_v2"


class EdgarClient:
    """Fetches and normalises SEC EDGAR XBRL fundamentals."""

    # Class-level rate-limit state shared across all instances in a process.
    # Protects against two concurrent EdgarClient instances together exceeding
    # the 10 req/s SEC hard limit.
    _rate_lock: threading.Lock = threading.Lock()
    _last_request_time: float = 0.0

    def __init__(self, operator_email: Optional[str] = None) -> None:
        email = operator_email or os.environ.get("OPERATOR_EMAIL", "contact@example.com")
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": f"RQIS-Fundamentals-Ingestion contact:{email}",
            "Accept-Encoding": "gzip, deflate",
        })

    # ── Public API ────────────────────────────────────────────────────────────

    def get_cik_map(self) -> dict[str, str]:
        """Return a mapping of ticker → zero-padded 10-digit CIK string.

        Fetches SEC's authoritative company_tickers.json (one call).
        """
        data = self._get_json(_TICKER_MAP_URL)
        cik_map: dict[str, str] = {}
        for entry in data.values():
            ticker = entry.get("ticker", "").upper()
            cik_int = entry.get("cik_str") or entry.get("cik")
            if ticker and cik_int is not None:
                cik_map[ticker] = str(cik_int).zfill(10)
        return cik_map

    def fetch_company_facts(self, cik: str) -> dict:
        """Fetch raw companyfacts JSON for a given zero-padded CIK."""
        url = _COMPANY_FACTS_URL.format(cik=cik)
        return self._get_json(url)

    def extract_fundamentals(
        self,
        ticker: str,
        facts: dict,
        period_types: Optional[set[str]] = None,
    ) -> list[dict]:
        """Extract normalised fundamental rows from a companyfacts JSON blob.

        Args:
            ticker: Ticker symbol (stored in result rows).
            facts: Raw JSON from fetch_company_facts().
            period_types: Set of period types to include ('annual', 'quarterly').
                Defaults to both.

        Returns:
            List of dicts matching the financial_statements schema:
            ticker, period_end_date, release_date, period_type, item_name,
            value, source, source_version.
        """
        if period_types is None:
            period_types = {"annual", "quarterly"}

        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        rows: list[dict] = []

        for item_name, concept_list in CONCEPT_ALIASES.items():
            extracted = _extract_concept(us_gaap, item_name, concept_list, ticker, period_types)
            rows.extend(extracted)

        # Derived items: compute from already-extracted base items.
        rows.extend(_compute_derived(rows, ticker))

        logger.debug(
            "edgar_extract_complete",
            ticker=ticker,
            rows=len(rows),
        )
        return rows

    def ingest_ticker(self, ticker: str, cik: str) -> int:
        """Fetch and return all fundamental rows for a single ticker.

        Returns the number of rows extracted (not necessarily written —
        callers handle persistence via TimescaleWriter).
        """
        try:
            facts = self.fetch_company_facts(cik)
        except requests.HTTPError as exc:
            logger.warning("edgar_fetch_failed", ticker=ticker, cik=cik, error=str(exc))
            return 0

        rows = self.extract_fundamentals(ticker, facts)
        logger.info("edgar_ingest_ticker", ticker=ticker, rows=len(rows))
        return len(rows)

    def backfill(
        self,
        tickers: list[str],
        cik_map: Optional[dict[str, str]] = None,
    ) -> dict[str, list[dict]]:
        """Backfill fundamentals for a list of tickers.

        Args:
            tickers: List of ticker symbols.
            cik_map: Preloaded ticker → CIK mapping.  Fetched automatically
                if not provided.

        Returns:
            Dict mapping ticker → list of fundamental row dicts.
            Tickers with no EDGAR match or fetch errors are absent.
        """
        if cik_map is None:
            logger.info("edgar_fetching_cik_map")
            cik_map = self.get_cik_map()

        results: dict[str, list[dict]] = {}
        missing = [t for t in tickers if t not in cik_map]
        if missing:
            logger.warning("edgar_no_cik_for_tickers", tickers=missing[:20], count=len(missing))

        for ticker in tickers:
            cik = cik_map.get(ticker.upper())
            if cik is None:
                continue
            try:
                facts = self.fetch_company_facts(cik)
                rows = self.extract_fundamentals(ticker, facts)
                if rows:
                    results[ticker] = rows
            except requests.HTTPError as exc:
                logger.warning("edgar_backfill_ticker_failed", ticker=ticker, error=str(exc))
            except Exception as exc:
                logger.error("edgar_backfill_unexpected_error", ticker=ticker, error=str(exc))

        logger.info(
            "edgar_backfill_complete",
            requested=len(tickers),
            succeeded=len(results),
            failed=len(tickers) - len(results),
        )
        return results

    # ── Internal HTTP ─────────────────────────────────────────────────────────

    def _get_json(self, url: str) -> dict:
        """GET JSON with class-level rate limiting and retry logic."""
        with EdgarClient._rate_lock:
            elapsed = time.monotonic() - EdgarClient._last_request_time
            if elapsed < _INTER_REQUEST_DELAY:
                time.sleep(_INTER_REQUEST_DELAY - elapsed)
            # Record time before release so no other thread can proceed
            # until the sleep is done.
            EdgarClient._last_request_time = time.monotonic()

        delays = (0,) + _RETRY_DELAYS
        last_exc: Exception | None = None

        for i, wait in enumerate(delays):
            if wait:
                time.sleep(wait)
            try:
                resp = self._session.get(url, timeout=30)
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code in (403, 404):
                    raise  # Don't retry auth or not-found errors
                last_exc = exc
                logger.warning("edgar_request_retry", url=url, attempt=i + 1, error=str(exc))
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("edgar_request_retry", url=url, attempt=i + 1, error=str(exc))

        raise requests.RequestException(f"Failed after {len(delays)} attempts: {url}") from last_exc


# ── Module-level helpers ───────────────────────────────────────────────────────

def _extract_concept(
    us_gaap: dict,
    item_name: str,
    concept_list: list[str],
    ticker: str,
    period_types: set[str],
) -> list[dict]:
    """Try each concept alias in order; return rows from the first that matches."""
    for concept_path in concept_list:
        # concept_path = "us-gaap/NetIncomeLoss" → key in us_gaap = "NetIncomeLoss"
        concept_key = concept_path.split("/", 1)[-1]
        concept_data = us_gaap.get(concept_key)
        if not concept_data:
            continue

        units = concept_data.get("units", {})
        # Monetary items → USD; share items → shares; ratios → pure
        for unit_key in ("USD", "shares", "pure"):
            observations = units.get(unit_key)
            if observations:
                rows = _parse_observations(
                    observations, ticker, item_name, period_types
                )
                if rows:
                    return rows

    return []


def _parse_observations(
    observations: list[dict],
    ticker: str,
    item_name: str,
    period_types: set[str],
) -> list[dict]:
    """Parse a list of EDGAR fact observations into financial_statements rows.

    Deduplicates on (end, filed, accn) — the same accession can repeat the
    same data point for the same period (e.g. a 10-K re-reporting prior-year
    comparatives) and would otherwise produce duplicate schema rows.
    """
    seen: set[tuple] = set()
    rows: list[dict] = []

    for obs in observations:
        form = obs.get("form", "")
        if form not in ACCEPTED_FORMS:
            continue

        filed_str = obs.get("filed")
        end_str = obs.get("end")
        val = obs.get("val")
        accn = obs.get("accn", "")

        if not filed_str or not end_str or val is None:
            continue

        dedup_key = (end_str, filed_str, accn)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        try:
            period_end = date.fromisoformat(end_str)
            release_dt = date.fromisoformat(filed_str)
        except ValueError:
            continue

        start_str = obs.get("start")
        fp = obs.get("fp", "")

        period_type = _classify_period(start_str, end_str, fp)
        if period_type is None or period_type not in period_types:
            continue

        rows.append({
            "ticker": ticker,
            "period_end_date": period_end,
            "release_date": release_dt,
            "period_type": period_type,
            "item_name": item_name,
            "value": Decimal(str(val)),
            "source": _SOURCE,
            "source_version": _SOURCE_VERSION,
        })

    return rows


def _classify_period(
    start_str: Optional[str],
    end_str: str,
    fp: str,
) -> Optional[str]:
    """Return 'annual', 'quarterly', or None if the period is unclassifiable."""
    if start_str:
        try:
            start = date.fromisoformat(start_str)
            end = date.fromisoformat(end_str)
            days = (end - start).days
        except ValueError:
            days = None

        if days is not None:
            if ANNUAL_MIN_DAYS <= days <= ANNUAL_MAX_DAYS:
                return "annual"
            if QUARTERLY_MIN_DAYS <= days <= QUARTERLY_MAX_DAYS:
                return "quarterly"
            return None  # TTM, semi-annual, etc.

    # Balance sheet items have no start date; classify by fp tag.
    if fp == "FY":
        return "annual"
    if fp in ("Q1", "Q2", "Q3", "Q4"):
        return "quarterly"
    return None


def _compute_derived(base_rows: list[dict], ticker: str) -> list[dict]:
    """Compute derived items (e.g. free_cash_flow) from base item rows.

    Groups by (period_end_date, period_type).  For each group, finds the
    latest available value for each required base item and uses the maximum
    of their release_dates as the derived item's release_date.  This ensures
    PIT correctness: the derived value is only available after the latest of
    its inputs was filed.

    If the two base items (e.g. operating_cash_flow and capex) were filed in
    separate submissions for the same period, this correctly defers the derived
    item's visibility to the later filing date.
    """
    derived: list[dict] = []

    for item_name, (formula, required) in DERIVED_ITEMS.items():
        # Group: (period_end_date, period_type) -> item_name -> list[(release_date, value)]
        groups: dict[tuple, dict[str, list[tuple]]] = {}
        for row in base_rows:
            if row["item_name"] not in required:
                continue
            key = (row["period_end_date"], row["period_type"])
            groups.setdefault(key, {}).setdefault(row["item_name"], []).append(
                (row["release_date"], row["value"])
            )

        for (period_end, period_type), items in groups.items():
            if not all(r in items for r in required):
                logger.debug(
                    "derived_item_missing_base",
                    ticker=ticker,
                    item_name=item_name,
                    period_end=str(period_end),
                    available=list(items.keys()),
                    required=required,
                )
                continue

            # Take the most-recently-filed value for each required item.
            best: dict[str, tuple] = {}  # item_name -> (release_date, value)
            for req_item in required:
                best[req_item] = max(items[req_item], key=lambda t: t[0])

            # Derived item is visible only after the latest of its inputs was filed.
            release_dt = max(v[0] for v in best.values())

            if item_name == "free_cash_flow":
                value = best["operating_cash_flow"][1] - best["capex"][1]
            else:
                continue  # future derived items added here

            derived.append({
                "ticker": ticker,
                "period_end_date": period_end,
                "release_date": release_dt,
                "period_type": period_type,
                "item_name": item_name,
                "value": value,
                "source": _SOURCE,
                "source_version": _SOURCE_VERSION,
            })

    return derived
