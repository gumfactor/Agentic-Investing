"""Wikipedia S&P 500 constituent-history provider.

Real-data provider selected per
docs/plans/01b2-constituent-source-contract.md. Fetches
https://en.wikipedia.org/wiki/List_of_S%26P_500_companies and parses:

1. The current-constituents table (``Symbol``, ``Security``, ``Date added``)
   — authoritative ``effective_start`` for every currently active ticker.
2. The "Selected changes" table (``Effective Date``, ``Added``, ``Removed``,
   ``Reason``) — membership-change events used to reconstruct closed-out
   historical intervals.

This module performs network I/O only inside ``fetch()``; it is never called
from pytest (tests use ``FixtureSP500Provider`` instead, per the operator
directive to keep automated tests offline-safe). It is invoked by the
operator-run ``scripts/import_universe_membership.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import structlog

from data.universe.providers.base import (
    ChangeEvent,
    CurrentConstituentRow,
    ParsedConstituentData,
    RawSnapshot,
)

logger = structlog.get_logger(__name__)

_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_UNIVERSE_ID = "sp500"
_SOURCE_VERSION = "wikipedia_sp500_v1"
# Wikipedia returns 403 to the default Python/urllib agent.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
_FOOTNOTE_PATTERN = re.compile(r"\[\d+\]")


def _clean_ticker(raw: str) -> str:
    return raw.strip().replace(".", "-").upper()


def _clean_text(raw: object) -> Optional[str]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = _FOOTNOTE_PATTERN.sub("", str(raw)).strip()
    return text or None


def _parse_effective_date(raw: object) -> Optional[date]:
    """Parse a Wikipedia date cell (``June 30, 2026`` or ``2026-06-30``)."""
    text = _clean_text(raw)
    if text is None:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


class WikipediaSP500Provider:
    """Real S&P 500 historical-constituents provider (see module docstring).

    Args:
        snapshot_path: optional path to a previously saved raw HTML snapshot
            (the ``raw.html`` written by ``persist_raw_snapshot``). When set,
            ``fetch()`` reads that file instead of performing network I/O,
            making an import reproducible from the checked-in artifact. The
            sibling ``manifest.json`` (when present) supplies the original
            ``retrieved_at`` and its checksum is verified before use.
    """

    provider_name = "wikipedia_sp500"

    def __init__(
        self,
        timeout_seconds: float = 20.0,
        snapshot_path: Optional[Path] = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._snapshot_path = snapshot_path

    def fetch(self) -> RawSnapshot:
        if self._snapshot_path is not None:
            return self._load_snapshot(self._snapshot_path)
        response = requests.get(_WIKIPEDIA_URL, headers=_HEADERS, timeout=self._timeout_seconds)
        response.raise_for_status()
        content = response.content
        retrieved_at = datetime.now(tz=timezone.utc)
        logger.info("wikipedia_sp500_fetched", n_bytes=len(content), retrieved_at=retrieved_at.isoformat())
        return RawSnapshot(
            provider_name=self.provider_name,
            source_version=_SOURCE_VERSION,
            retrieved_at=retrieved_at,
            content=content,
            content_type="text/html",
            origin_url=_WIKIPEDIA_URL,
        )

    def _load_snapshot(self, path: Path) -> RawSnapshot:
        content = path.read_bytes()
        manifest_path = path.parent / "manifest.json"
        retrieved_at = datetime.now(tz=timezone.utc)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            expected = manifest.get("checksum_sha256")
            actual = hashlib.sha256(content).hexdigest()
            if expected and expected != actual:
                raise ValueError(
                    f"Snapshot checksum mismatch for {path}: manifest says {expected}, "
                    f"file hashes to {actual}. The artifact has been modified; refusing to import."
                )
            retrieved_at = datetime.fromisoformat(manifest["retrieved_at"])
        else:
            logger.warning(
                "wikipedia_sp500_snapshot_without_manifest",
                path=str(path),
                note="using current time as retrieved_at",
            )
        logger.info(
            "wikipedia_sp500_snapshot_loaded",
            path=str(path),
            n_bytes=len(content),
            retrieved_at=retrieved_at.isoformat(),
        )
        return RawSnapshot(
            provider_name=self.provider_name,
            source_version=_SOURCE_VERSION,
            retrieved_at=retrieved_at,
            content=content,
            content_type="text/html",
            origin_url=_WIKIPEDIA_URL,
        )

    def parse(self, raw: RawSnapshot) -> ParsedConstituentData:
        tables = pd.read_html(io.StringIO(raw.content.decode("utf-8")))
        if len(tables) < 2:
            raise ValueError(
                f"Expected at least 2 tables on the Wikipedia S&P 500 page, found {len(tables)}. "
                "The page structure may have changed; the parser needs review before this "
                "snapshot can be imported."
            )

        current_table = tables[0]
        changes_table = tables[1]

        current_rows = self._parse_current_table(current_table)
        change_events = self._parse_changes_table(changes_table)

        logger.info(
            "wikipedia_sp500_parsed",
            n_current_rows=len(current_rows),
            n_change_events=len(change_events),
        )
        return ParsedConstituentData(
            universe_id=_UNIVERSE_ID,
            current_rows=current_rows,
            change_events=change_events,
        )

    @staticmethod
    def _parse_current_table(df: pd.DataFrame) -> list[CurrentConstituentRow]:
        required = {"Symbol", "Security", "Date added"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"Wikipedia current-constituents table is missing columns {missing}; "
                "the page structure may have changed."
            )
        rows: list[CurrentConstituentRow] = []
        for _, r in df.iterrows():
            ticker = _clean_text(r["Symbol"])
            eff_date = _parse_effective_date(r["Date added"])
            if ticker is None or eff_date is None:
                # A current-constituents row with no parseable addition date
                # cannot be certified historically; skip it rather than guess.
                logger.warning(
                    "wikipedia_sp500_current_row_skipped",
                    symbol=r.get("Symbol"),
                    date_added=r.get("Date added"),
                )
                continue
            rows.append(
                CurrentConstituentRow(
                    ticker=_clean_ticker(ticker),
                    security_name=_clean_text(r["Security"]) or ticker,
                    effective_start=eff_date,
                    source_record_id=f"current-{_clean_ticker(ticker)}",
                )
            )
        return rows

    @staticmethod
    def _parse_changes_table(df: pd.DataFrame) -> list[ChangeEvent]:
        # pandas.read_html returns a MultiIndex for this table:
        # ('Effective Date','Effective Date'), ('Added','Ticker'),
        # ('Added','Security'), ('Removed','Ticker'), ('Removed','Security'),
        # ('Reason','Reason'). Validate the ACTUAL header text positionally
        # before renaming (adversarial-review fix: a positional rename with a
        # post-hoc check of the names we just assigned was tautological — a
        # silently reordered page would have mis-assigned every column).
        expected_multiindex = [
            ("effective date", "effective date"),
            ("added", "ticker"),
            ("added", "security"),
            ("removed", "ticker"),
            ("removed", "security"),
            ("reason", "reason"),
        ]
        columns = df.columns
        if isinstance(columns, pd.MultiIndex):
            actual = [
                (str(a).strip().lower(), str(b).strip().lower()) for a, b in columns
            ]
            if actual != expected_multiindex:
                raise ValueError(
                    "Wikipedia 'Selected changes' table headers changed or were "
                    f"reordered: expected {expected_multiindex}, found {actual}. "
                    "Refusing to import until the parser is reviewed against the "
                    "new page structure."
                )
            df = df.copy()
            df.columns = [
                "effective_date", "added_ticker", "added_security",
                "removed_ticker", "removed_security", "reason",
            ]
        else:
            rename_map = {
                "Effective Date": "effective_date",
                "Added Ticker": "added_ticker",
                "Added Security": "added_security",
                "Removed Ticker": "removed_ticker",
                "Removed Security": "removed_security",
                "Reason": "reason",
            }
            unexpected = set(df.columns) - set(rename_map)
            if unexpected:
                raise ValueError(
                    "Wikipedia 'Selected changes' table has unexpected flat "
                    f"headers {sorted(unexpected)}; expected only "
                    f"{sorted(rename_map)}. Refusing to import until the parser "
                    "is reviewed against the new page structure."
                )
            df = df.rename(columns=rename_map)

        required = {"effective_date", "added_ticker", "removed_ticker"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"Wikipedia 'Selected changes' table is missing columns {missing}; "
                "the page structure may have changed."
            )

        events: list[ChangeEvent] = []
        for idx, r in df.iterrows():
            eff_date = _parse_effective_date(r["effective_date"])
            if eff_date is None:
                logger.warning("wikipedia_sp500_change_row_skipped_no_date", row_index=idx)
                continue
            added_ticker = _clean_text(r.get("added_ticker"))
            removed_ticker = _clean_text(r.get("removed_ticker"))
            if added_ticker is None and removed_ticker is None:
                continue
            events.append(
                ChangeEvent(
                    effective_date=eff_date,
                    added_ticker=_clean_ticker(added_ticker) if added_ticker else None,
                    added_security_name=_clean_text(r.get("added_security")),
                    removed_ticker=_clean_ticker(removed_ticker) if removed_ticker else None,
                    removed_security_name=_clean_text(r.get("removed_security")),
                    reason=_clean_text(r.get("reason")),
                    source_record_id=f"chg-{idx}",
                )
            )
        return events


