"""FIXTURE constituent provider — synthetic data for tests only.

NEVER treat this as real S&P 500 membership. ``universe_id`` is deliberately
"sp500_fixture" (not "sp500") so a fixture import can never be mistaken for
the real Wikipedia-sourced import in the same database.

Scenario coverage (see docs/plans/01b2-constituent-source-contract.md and
the 01B-2 acceptance tests in ``data/tests/universe/test_acceptance_1_4.py``):

- ``AAA``: always-active member since the start of the coverage window.
- ``BBB``: added, then removed — the "removed constituent" case.
- ``CCC``: added after the coverage window start — the "entrant" case.
- ``DDD``: added, removed, then re-added (re-entry) with a different open
  interval — the "remove-then-re-enter" case. Its first stint is also
  duplicated in the "current constituents"-shaped addition event to exercise
  the importer's dedup of a redundant current-vs-changes addition date,
  matching real Wikipedia data (a currently active ticker's original
  addition also appears once in the changes table).
- ``EEE`` -> ``FFF``: a same-day ticker-symbol rename — the "ticker change"
  case, resolved via ``universe_symbol_history`` rather than a membership
  gap.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

from data.universe.providers.base import (
    ChangeEvent,
    ConstituentProvider,
    CurrentConstituentRow,
    ParsedConstituentData,
    RawSnapshot,
)

FIXTURE_UNIVERSE_ID = "sp500_fixture"
FIXTURE_SOURCE_VERSION = "fixture-v1"
FIXTURE_COVERAGE_START = date(2020, 1, 1)


class FixtureSP500Provider:
    """Deterministic synthetic provider. Clearly labeled FIXTURE — no real data."""

    provider_name = "fixture_sp500"

    def __init__(self, retrieved_at: datetime | None = None) -> None:
        self._retrieved_at = retrieved_at or datetime(2024, 1, 2, tzinfo=timezone.utc)

    def fetch(self) -> RawSnapshot:
        payload = {
            "fixture": True,
            "note": "Synthetic FIXTURE data for 01B-2 tests. Not real S&P 500 history.",
            "current_rows": [
                {"ticker": "AAA", "security_name": "Alpha Fixture Co", "effective_start": "2020-01-01"},
                {"ticker": "CCC", "security_name": "Charlie Fixture Co", "effective_start": "2021-06-01"},
                {"ticker": "DDD", "security_name": "Delta Fixture Co (re-entry)", "effective_start": "2022-01-01"},
                {"ticker": "FFF", "security_name": "Foxtrot Fixture Co (renamed from EEE)", "effective_start": "2021-03-01"},
            ],
            "change_events": [
                # AAA's original addition duplicated in the changes table,
                # matching real Wikipedia redundancy for still-active names.
                {"effective_date": "2020-01-01", "added_ticker": "AAA", "added_security_name": "Alpha Fixture Co", "removed_ticker": None, "removed_security_name": None, "reason": "Index reconstitution.", "source_record_id": "chg-0"},
                # DDD's first (now-closed) stint.
                {"effective_date": "2020-01-01", "added_ticker": "DDD", "added_security_name": "Delta Fixture Co", "removed_ticker": None, "removed_security_name": None, "reason": "Index reconstitution.", "source_record_id": "chg-1"},
                {"effective_date": "2020-04-01", "added_ticker": None, "added_security_name": None, "removed_ticker": "DDD", "removed_security_name": "Delta Fixture Co", "reason": "Market capitalization change.", "source_record_id": "chg-2"},
                # BBB: fully closed stint.
                {"effective_date": "2020-06-01", "added_ticker": "BBB", "added_security_name": "Bravo Fixture Co", "removed_ticker": None, "removed_security_name": None, "reason": "Index reconstitution.", "source_record_id": "chg-3"},
                {"effective_date": "2021-01-01", "added_ticker": None, "added_security_name": None, "removed_ticker": "BBB", "removed_security_name": "Bravo Fixture Co", "reason": "Market capitalization change.", "source_record_id": "chg-4"},
                # EEE's original addition.
                {"effective_date": "2020-01-01", "added_ticker": "EEE", "added_security_name": "Echo Fixture Co", "removed_ticker": None, "removed_security_name": None, "reason": "Index reconstitution.", "source_record_id": "chg-5"},
                # EEE -> FFF ticker rename (same effective date, both sides populated).
                {"effective_date": "2021-03-01", "added_ticker": "FFF", "added_security_name": "Foxtrot Fixture Co", "removed_ticker": "EEE", "removed_security_name": "Echo Fixture Co", "reason": "Echo Fixture Co changed its ticker symbol from EEE to FFF.", "source_record_id": "chg-6"},
                # CCC's addition (also duplicated vs. the current table).
                {"effective_date": "2021-06-01", "added_ticker": "CCC", "added_security_name": "Charlie Fixture Co", "removed_ticker": None, "removed_security_name": None, "reason": "Index reconstitution.", "source_record_id": "chg-7"},
                # DDD's re-entry.
                {"effective_date": "2022-01-01", "added_ticker": "DDD", "added_security_name": "Delta Fixture Co (re-entry)", "removed_ticker": None, "removed_security_name": None, "reason": "Index reconstitution.", "source_record_id": "chg-8"},
            ],
        }
        content = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        return RawSnapshot(
            provider_name=self.provider_name,
            source_version=FIXTURE_SOURCE_VERSION,
            retrieved_at=self._retrieved_at,
            content=content,
            content_type="application/json",
            origin_url=None,
        )

    def parse(self, raw: RawSnapshot) -> ParsedConstituentData:
        payload = json.loads(raw.content.decode("utf-8"))
        current_rows = [
            CurrentConstituentRow(
                ticker=row["ticker"],
                security_name=row["security_name"],
                effective_start=date.fromisoformat(row["effective_start"]),
                source_record_id=f"current-{row['ticker']}",
            )
            for row in payload["current_rows"]
        ]
        change_events = [
            ChangeEvent(
                effective_date=date.fromisoformat(evt["effective_date"]),
                added_ticker=evt["added_ticker"],
                added_security_name=evt["added_security_name"],
                removed_ticker=evt["removed_ticker"],
                removed_security_name=evt["removed_security_name"],
                reason=evt["reason"],
                source_record_id=evt["source_record_id"],
            )
            for evt in payload["change_events"]
        ]
        return ParsedConstituentData(
            universe_id=FIXTURE_UNIVERSE_ID,
            current_rows=current_rows,
            change_events=change_events,
        )


def fixture_checksum(raw: RawSnapshot) -> str:
    return hashlib.sha256(raw.content).hexdigest()
