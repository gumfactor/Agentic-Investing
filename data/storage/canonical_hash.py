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
from decimal import Decimal
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


def _normalize_float(value: float) -> str:
    """Canonical string for a floating value.

    Normalizes negative zero to positive zero so that `0.0` and `-0.0`
    (which compare equal but ``repr`` differently, e.g. from ``0.1 - 0.1``)
    hash identically -- a spec-named determinism requirement (section 2.1).
    """
    if math.isnan(value):
        return ""
    if value == 0:  # collapses both 0.0 and -0.0
        value = 0.0
    return repr(float(value))


def _normalize_value(value: Any) -> str:
    """Render one cell to its canonical string form."""
    if value is None:
        return ""
    if isinstance(value, (bool, np.bool_)):
        return "true" if value else "false"
    if isinstance(value, (float, np.floating)):
        return _normalize_float(float(value))
    if isinstance(value, Decimal):
        # Prices are stored NUMERIC(18,6); pandas may hand us Decimal objects
        # (e.g. when read without coerce_float). Route through the float
        # canonicalization so Decimal("100.500000") and float 100.5 -- the
        # same logical value -- produce the same canonical string. NaN
        # Decimals are treated as missing, mirroring the float path.
        if value.is_nan():
            return ""
        return _normalize_float(float(value))
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

    # Column order is always the alphabetical column ordering, so equal
    # values hash identically regardless of the source frame's column order.
    ordered_cols = sorted(normalized.columns)
    normalized = normalized[ordered_cols]

    # Row order must be a pure function of content. Sort by the declared
    # per-data-type key first, then break ties on ALL remaining columns, so
    # rows sharing a declared sort key (e.g. two corporate actions with the
    # same (ticker, ex_date, action_type)) still order deterministically
    # instead of inheriting the input frame's incidental row order. For an
    # unknown data_type (P2 note) the fallback sorts on the full set of
    # normalized-STRING columns lexicographically -- not on the original
    # typed values -- which is fine since identity here is defined over the
    # canonical string forms, not the source dtypes.
    declared = [c for c in CANONICAL_SORT_KEYS.get(data_type, []) if c in ordered_cols]
    tiebreak = [c for c in ordered_cols if c not in declared]
    full_sort_cols = declared + tiebreak
    normalized = normalized.sort_values(full_sort_cols, kind="mergesort").reset_index(drop=True)

    rows = normalized.apply(lambda row: _FIELD_SEP.join(row), axis=1)
    return hashlib.sha256(_ROW_SEP.join(rows).encode("utf-8")).hexdigest()


def bytes_sha256(payload: bytes) -> str:
    """Secondary, informational SHA-256 of raw uploaded bytes (never a key/gate)."""
    return hashlib.sha256(payload).hexdigest()
