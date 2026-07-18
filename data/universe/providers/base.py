"""Provider-neutral data shapes for the constituent import pipeline.

Any historical-constituents source (Wikipedia today; Polygon or another
commercial feed at Gate 03) implements ``ConstituentProvider``. The import
pipeline (``data/universe/import_pipeline.py``) only depends on these types,
never on a specific provider's HTML/JSON shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, Protocol


@dataclass(frozen=True)
class RawSnapshot:
    """The raw, unparsed response from a provider, plus retrieval provenance.

    Persisted to disk with a checksum before any parsing happens (§1.2 step 1
    — "save the raw source response/file with a checksum and source
    version").
    """

    provider_name: str
    source_version: str
    retrieved_at: datetime
    content: bytes
    content_type: str  # e.g. "text/html", "application/json"
    origin_url: Optional[str] = None


@dataclass(frozen=True)
class CurrentConstituentRow:
    """One row of a provider's "current constituents" listing, if it has one."""

    ticker: str
    security_name: str
    effective_start: date
    source_record_id: str


@dataclass(frozen=True)
class ChangeEvent:
    """One membership-change event from a provider's change history.

    A single event may carry both an ``added_ticker`` and a
    ``removed_ticker`` (the common case for an index reconstitution, and the
    shape a same-day ticker rename takes before the importer reclassifies
    it — see ``import_pipeline.build_staging_records``).
    """

    effective_date: date
    added_ticker: Optional[str]
    added_security_name: Optional[str]
    removed_ticker: Optional[str]
    removed_security_name: Optional[str]
    reason: Optional[str]
    source_record_id: str
    announced_at: Optional[datetime] = None


@dataclass(frozen=True)
class ParsedConstituentData:
    """Provider-neutral parse result: everything the import pipeline needs."""

    universe_id: str
    current_rows: list[CurrentConstituentRow] = field(default_factory=list)
    change_events: list[ChangeEvent] = field(default_factory=list)


class ConstituentProvider(Protocol):
    """Protocol every historical-constituents source implements."""

    provider_name: str

    def fetch(self) -> RawSnapshot:
        """Fetch (or load, for a fixture) the raw source response."""
        ...

    def parse(self, raw: RawSnapshot) -> ParsedConstituentData:
        """Parse a raw snapshot into provider-neutral staging inputs."""
        ...
