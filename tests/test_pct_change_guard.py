"""Repository guard for BUG-010 (missing-data return policy).

Fails with an actionable file/line message if a production price-return path
calls ``pandas`` ``.pct_change(`` without an explicit ``fill_method=None``
argument on the same call. Pandas' legacy default (``fill_method='pad'``)
forward-fills a missing session's price before differencing, which turns a
genuine data gap into a fabricated, non-NaN return — the defect tracked as
BUG-010 in bugs.md and fixed by roadmap item 01B-1 (see
docs/plans/01b1-pct-change-inventory.md for the full call-site inventory and
migration record).

Scope: every production Python directory that could plausibly hold a
price/NAV return calculation — ``signals/``, ``backtesting/``,
``portfolio/``, ``reporting/``, ``execution/``, ``risk/``, ``airflow/``,
``scripts/``, ``data/`` — excluding test directories and any call site
explicitly listed in ``_DOCUMENTED_EXCEPTIONS`` below with a recorded,
reviewed rationale (none exist as of 01B-1; the inventory doc records why
every current call site is a real price/NAV return, not an exception).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories scanned for production price-return code. Covers every
# production directory that could plausibly grow a price/NAV return
# calculation — including ones with zero current call sites (execution,
# risk, airflow, scripts, data) so a new unguarded call added there is
# caught immediately rather than requiring a scope update first.
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
]

# Any directory component matching one of these is skipped entirely (tests,
# caches, etc. are not "production" code paths for this guard).
_EXCLUDED_DIR_PARTS = {"tests", "test", "__pycache__", ".git"}

# (relative POSIX path, 1-based line number): reason. A call site listed here
# is a reviewed, documented exception to the default policy (e.g. a return
# series that is gap-free by construction). Empty as of 01B-1 — every
# pct_change() call site found in scope is a genuine price/NAV return and was
# migrated to use fill_method=None (or the shared signals/indicators/
# _price_utils.daily_return helper) rather than exempted.
_DOCUMENTED_EXCEPTIONS: dict[tuple[str, int], str] = {}

# A call is compliant if `fill_method=None` appears literally on the same
# line as `.pct_change(`. All call sites in this codebase are single-line
# calls, so a same-line check is sufficient and keeps the guard simple and
# auditable; if a future multi-line call is added, prefer reformatting it to
# one line over weakening this guard.
_PCT_CHANGE_RE = re.compile(r"\.pct_change\(")
_COMPLIANT_RE = re.compile(r"\.pct_change\([^)]*fill_method\s*=\s*None")


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


def test_no_unguarded_pct_change_in_production_price_return_paths():
    violations: list[str] = []
    for path in _iter_python_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not _PCT_CHANGE_RE.search(line):
                continue
            if _COMPLIANT_RE.search(line):
                continue
            reason = _DOCUMENTED_EXCEPTIONS.get((rel, lineno))
            if reason is not None:
                continue
            violations.append(
                f"{rel}:{lineno}: pct_change() call without fill_method=None "
                f"and no entry in _DOCUMENTED_EXCEPTIONS "
                f"-> {line.strip()!r}"
            )

    if violations:
        pytest.fail(
            "BUG-010 guard: found pct_change() call(s) that can silently "
            "forward-fill a missing price into a fabricated return. Use "
            "pct_change(fill_method=None), the "
            "signals.indicators._price_utils.daily_return helper, or add a "
            "reviewed, reasoned entry to _DOCUMENTED_EXCEPTIONS in "
            "tests/test_pct_change_guard.py if this is a genuine non-price "
            "exception:\n" + "\n".join(violations)
        )
