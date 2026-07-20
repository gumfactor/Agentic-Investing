"""Canonical logical-content hashing for content-addressed snapshots (03A-1).

Implements docs/plans/03a-immutable-research-data-design.md section 2.1's
"canonical logical content hash" procedure, generalized from the pre-existing
`backtesting.dataset_manifest._alpha_scores_hash` (which only covered
`alpha_scores`) to all four bundle data types.

Content identity is logical, not byte-level (PM amendment 1, option a):
parquet's serialized bytes are not deterministic across writer/library
versions, footer metadata, compression settings, or incidental row order,
even when the underlying values are identical. A byte-derived key would give
two pins of identical data two different keys and break idempotency. Instead:

  1. sort rows by a per-data-type canonical key (falling back to sorting by
     every column, alphabetically, if the data_type is unknown or none of
     its declared key columns are present);
  2. sort columns by name;
  3. normalize every value to one canonical string representation --
     dates/timestamps to ISO-8601, floats via `repr(float(x))`, ints via
     `str(int(x))`, bools to `"true"`/`"false"`, missing values to `""` --
     so pandas/pyarrow dtype drift between environments (e.g. `date` objects
     vs. `datetime64[ns]` vs. a parquet round-trip's `Timestamp`) cannot
     change the hash of logically equal values;
  4. hash the resulting canonical row-string stream with SHA-256.

Parquet remains the carrier format only; this hash is the object's identity.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

# Per-data-type canonical row sort key, mirroring
# backtesting.dataset_manifest._DATE_COL / the design plan section 2.1 list.
CANONICAL_SORT_KEYS: dict[str, list[str]] = {
    "alpha_scores": ["score_date", "ticker"],
    "daily_prices": ["ticker", "date"],
    "benchmark": ["ticker", "date"],
    "corporate_actions": ["ticker", "ex_date", "action_type"],
}

# Field/row separators chosen to avoid collision with ordinary text data
# (unlike "|", which could plausibly appear in a free-text column).
_FIELD_SEP = "\x1f"
_ROW_SEP = "\x1e"

EMPTY_CONTENT_SHA256 = hashlib.sha256(b"").hexdigest()


def _normalize_value(value: Any) -> str:
    """Render one cell to its canonical string form."""
    if value is None:
        return ""
    if isinstance(value, (bool, np.bool_)):
        return "true" if value else "false"
    if isinstance(value, (float, np.floating)):
        if math.isnan(value):
            return ""
        return repr(float(value))
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def canonical_content_sha256(df: pd.DataFrame | None, data_type: str) -> str:
    """SHA-256 of a DataFrame's canonical logical content.

    Deterministic under row order, column order, and equivalent dtype
    representations of the same logical values (section 2.1's "shuffle rows/
    columns or round-trip through parquet -> same hash" requirement).

    An empty or ``None`` DataFrame returns the SHA-256 of an empty byte
    string -- a valid sentinel distinct from any real hash.
    """
    if df is None or df.empty:
        return EMPTY_CONTENT_SHA256

    normalized = df.apply(lambda col: col.map(_normalize_value))

    sort_cols = [c for c in CANONICAL_SORT_KEYS.get(data_type, []) if c in normalized.columns]
    if not sort_cols:
        sort_cols = sorted(normalized.columns)

    ordered_cols = sorted(normalized.columns)
    normalized = normalized[ordered_cols]
    normalized = normalized.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)

    rows = normalized.apply(lambda row: _FIELD_SEP.join(row), axis=1)
    return hashlib.sha256(_ROW_SEP.join(rows).encode("utf-8")).hexdigest()


def bytes_sha256(payload: bytes) -> str:
    """Secondary, informational SHA-256 of raw uploaded bytes (never a key/gate)."""
    return hashlib.sha256(payload).hexdigest()
