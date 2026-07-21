"""Shared pytest fixtures for the RQIS test suite.

This module currently exists to address BUG-081 (see ``bugs.md``): systemic
paper-path test fragility to process-global state (``os.environ``, cwd) and
wall-clock time, both of which surfaced as order-dependent / time-dependent
flakes during parallel development waves (BUG-080 and a related
``date.today()`` boundary flake in ``test_paper_submit_reconcile_check.py``).

Rather than continuing to patch victim test files one at a time, the fixture
below is scoped to the whole family of ``tests/test_paper_*.py`` modules
(the "paper-path" tests that exercise the Phase 4/5 paper-trading preflight
scripts under ``scripts/paper_*.py``). It supersedes and retires the
one-off, per-file autouse fixture that BUG-080 added to
``tests/test_paper_stage_blotter_check.py``.

Scoping is deliberately narrow (filename prefix, not a blanket
``conftest.py``-wide autouse fixture) so this does not snapshot/restore
env/cwd for the rest of the ~2400-test suite, which could mask or interact
with other tests' legitimate env/cwd manipulation elsewhere in the repo.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import pytest

# Filename prefix identifying the paper-path test modules that need
# env/cwd isolation. These modules exercise `scripts/paper_*.py`, several of
# which call `load_dotenv()` (mutating `os.environ` from the repository
# `.env` as a side effect) and/or resolve paths relative to the current
# working directory.
_PAPER_PATH_TEST_PREFIX = "test_paper_"

# A single frozen instant shared by paper-path tests that need "now" for
# both constructing fixture data (e.g. a blotter's `generated_at_utc`) and
# for injecting into a script's `now_fn=`/`today_fn=` parameter. Deriving
# both from one frozen constant means the two computations can never drift
# across a real wall-clock boundary (e.g. a UTC midnight rollover) during a
# slow or parallel test run -- which is exactly what caused the BUG-081
# `test_confirm_yes_submits_with_fake_broker_and_writes_reconciliation`
# flake (calendar rolled over mid-run).
#
# This repo's existing convention for deterministic clocks is dependency
# injection (`today_fn: Callable[[], date] = date.today`, `now_fn: ... =
# lambda: datetime.now(UTC)` -- see scripts/paper_*.py) rather than
# monkeypatching the datetime module itself (e.g. freezegun, even though it
# is present in requirements-dev.txt for other uses). These fixtures follow
# that same injected-clock convention instead of introducing a new mocking
# style for this one bug class.
FROZEN_NOW_UTC = datetime(2026, 6, 20, 15, 0, tzinfo=UTC)


def _is_paper_path_test(fspath: Path) -> bool:
    return fspath.name.startswith(_PAPER_PATH_TEST_PREFIX) and fspath.suffix == ".py"


@pytest.fixture(autouse=True)
def _paper_path_isolate_global_state(request: pytest.FixtureRequest):
    """Snapshot/restore ``os.environ`` and cwd around every paper-path test.

    Only activates for modules named ``tests/test_paper_*.py`` (see
    ``_is_paper_path_test``); it is a no-op for every other test in the
    suite. This addresses BUG-081 and supersedes the per-file fixture that
    BUG-080 added only to ``tests/test_paper_stage_blotter_check.py``.
    """
    if not _is_paper_path_test(Path(str(request.node.fspath))):
        yield
        return

    saved_environ = dict(os.environ)
    saved_cwd = os.getcwd()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)
        try:
            os.chdir(saved_cwd)
        except OSError:
            # The saved directory may no longer exist (e.g. a removed
            # tmp_path); there is nothing sane to restore to in that case.
            pass


@pytest.fixture
def frozen_now_utc() -> datetime:
    """A fixed, timezone-aware UTC ``datetime`` for paper-path date-guard tests.

    Use together with ``frozen_now_fn`` (or ``lambda: frozen_now_utc``) so
    that any "now" used to build test fixture data and any "now" injected
    into the script under test via ``now_fn=`` are the exact same instant,
    eliminating wall-clock/date-boundary dependence entirely (BUG-081).
    """
    return FROZEN_NOW_UTC


@pytest.fixture
def frozen_now_fn(frozen_now_utc: datetime) -> Callable[[], datetime]:
    """A zero-arg callable returning ``frozen_now_utc``, for ``now_fn=`` injection."""
    return lambda: frozen_now_utc
