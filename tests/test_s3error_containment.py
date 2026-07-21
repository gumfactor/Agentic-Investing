"""Repository guard for the 03A-2 fail-closed object-store error taxonomy.

Fails with an actionable file/line message if any production module outside
``data/storage/parquet_snapshots.py`` imports or catches
``minio.error.S3Error`` directly. ``data/storage/parquet_snapshots.py`` is
the single translation boundary from raw MinIO errors to the typed
hierarchy in ``data/storage/errors.py`` (``SnapshotNotFoundError``,
``SnapshotStoreUnavailableError``, ``SnapshotAccessDeniedError``,
``SnapshotPartialReadError``, ``SnapshotIntegrityError`` -- design plan
section 4.2). Any other module reaching around that boundary risks
re-introducing BUG-039's collapse of every S3 failure mode (timeout, auth,
bucket-policy) into a single "no data" outcome.

Mirrors the call-site-inventory guard pattern in
``tests/test_pct_change_guard.py`` (01B-1 / BUG-010).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories scanned for production object-store access code.
_SCAN_DIRS = [
    "signals",
    "backtesting",
    "portfolio",
    "reporting",
    "execution",
    "risk",
    "airflow",
    "scripts",
    "data",
    "strategy_registry",
]

_EXCLUDED_DIR_PARTS = {"tests", "test", "__pycache__", ".git"}

# The single translation boundary module (relative POSIX path). This module
# is allowed -- and required -- to import/catch S3Error directly.
_BOUNDARY_MODULE = "data/storage/parquet_snapshots.py"

# Matches actual code usage of S3Error (import, except clause, isinstance
# check, or constructing/raising it) -- not comments/docstrings that merely
# mention the name in prose (this file's own module docstrings, and the
# explanatory comments in data/storage/errors.py and
# backtesting/dataset_manifest.py about *why* only the boundary module may
# touch it, are expected hits on a bare `\bS3Error\b` search but are not
# code-level violations).
_S3ERROR_REF_RE = re.compile(
    r"(from\s+minio\.error\s+import\s+S3Error)"
    r"|(except\s+[^:]*\bS3Error\b)"
    r"|(isinstance\([^)]*\bS3Error\b)"
    r"|(raise\s+S3Error\b)"
    r"|(:\s*S3Error\b)"
)


def _is_comment_or_blank(line: str) -> bool:
    return not line.strip() or line.strip().startswith("#")


# Backtick-quoted spans (` `...` `) are prose references inside docstrings
# (e.g. "the previous blanket `except S3Error: raise FileNotFoundError`
# behavior") rather than live code -- strip them before matching so
# documentation about the old/forbidden pattern doesn't trip the guard.
_BACKTICK_SPAN_RE = re.compile(r"`[^`]*`")

# (relative POSIX path, 1-based line number): reason. A reviewed, documented
# exception to the "only the boundary module touches S3Error" rule. Empty as
# of 03A-2 -- every reference to S3Error outside the boundary module was
# migrated to the typed hierarchy rather than exempted.
_DOCUMENTED_EXCEPTIONS: dict[tuple[str, int], str] = {}


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for scan_dir in _SCAN_DIRS:
        base = _REPO_ROOT / scan_dir
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rel_parts = path.relative_to(_REPO_ROOT).parts
            if any(part in _EXCLUDED_DIR_PARTS for part in rel_parts):
                continue
            files.append(path)
    return files


def test_no_s3error_reference_outside_translation_boundary():
    violations: list[str] = []
    for path in _iter_python_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel == _BOUNDARY_MODULE:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _is_comment_or_blank(line):
                continue
            code_like = _BACKTICK_SPAN_RE.sub("", line)
            if not _S3ERROR_REF_RE.search(code_like):
                continue
            reason = _DOCUMENTED_EXCEPTIONS.get((rel, lineno))
            if reason is not None:
                continue
            violations.append(f"{rel}:{lineno}: {line.strip()!r}")

    if violations:
        pytest.fail(
            "03A-2 guard: found minio.error.S3Error referenced outside the "
            f"single translation boundary ({_BOUNDARY_MODULE}). Catch/raise "
            "the typed hierarchy in data.storage.errors instead "
            "(SnapshotNotFoundError, SnapshotStoreUnavailableError, "
            "SnapshotAccessDeniedError, SnapshotPartialReadError, "
            "SnapshotIntegrityError), or use "
            "data.storage.parquet_snapshots.get_object_bytes /"
            "translate_object_store_error, or add a reviewed, reasoned "
            "entry to _DOCUMENTED_EXCEPTIONS in "
            "tests/test_s3error_containment.py if this is a genuine "
            "exception:\n" + "\n".join(violations)
        )
