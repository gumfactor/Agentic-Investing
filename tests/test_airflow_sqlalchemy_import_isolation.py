"""Repo-wide guard: no Airflow-reachable code path may import the
SQLAlchemy-2-only ORM modules (data.research.identity, data.research.models,
data.universe.runtime, data.universe.models, data.universe.import_pipeline).

BUG-009 section 4 / adversarial-review history on PR #35 (01B-3):

- Round 2 found ``airflow/dags/daily_signal_pipeline.py::_write_scores``
  imported ``data.research.identity`` (SQLAlchemy-2-only ORM), which would
  raise ``ImportError`` the moment that task actually executed inside the
  packaged Airflow image (SQLAlchemy 1.4.51 pinned — see
  ``infra/docker/Dockerfile.airflow``), despite passing every test in this
  repo's SQLAlchemy 2.x dev environment. Fixed with a local plain-SQL
  lookup.
- Round 5 found the IDENTICAL bug reintroduced at a second call site,
  ``scripts/paper_inputs_check.py``, which is Airflow-reachable via
  ``airflow/dags/daily_paper_trading.py``'s ``_verify_inputs``/
  ``_construct_target`` tasks (``from scripts.paper_inputs_check import
  run as inputs_run`` etc.) — reached because a later fix round added a new
  active-run filter there without reusing the round-2 fix.

This module is a repo-wide, generic version of the DAG-local
``TestImportIsolation`` check in ``tests/test_daily_signal_pipeline_pit.py``
(which only covers ``daily_signal_pipeline.py`` itself): it statically
walks every DAG file under ``airflow/dags/`` for ``from scripts.X import``
statements (anywhere — module level or inside a task function; Airflow
tasks lazily import their dependencies as a matter of course), resolves
each target to its source file under ``scripts/``, and asserts neither the
DAG file nor any transitively-reachable script imports a banned module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DAGS_DIR = _REPO_ROOT / "airflow" / "dags"
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

_BANNED_MODULES = (
    "data.universe.runtime",
    "data.universe.models",
    "data.universe.import_pipeline",
    "data.research.identity",
    "data.research.models",
)
_BANNED_PACKAGES = ("data.universe", "data.research")


def _dag_files() -> list[Path]:
    return sorted(p for p in _DAGS_DIR.glob("*.py") if p.name != "__init__.py")


def _find_banned_imports(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.startswith(_BANNED_MODULES)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(_BANNED_MODULES) or node.module in _BANNED_PACKAGES:
                offenders.append(node.module)
    return offenders


def _find_scripts_imports(tree: ast.AST) -> set[str]:
    """Every 'scripts.<module>' target imported anywhere in this file
    (module level or inside a function — Airflow tasks lazily import)."""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("scripts."):
            targets.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("scripts."):
                    targets.add(alias.name)
    return targets


def _script_module_path(module_name: str) -> Path:
    # "scripts.paper_inputs_check" -> scripts/paper_inputs_check.py
    relative = module_name.split(".", 1)[1]
    return _SCRIPTS_DIR / f"{relative}.py"


class TestNoDagReachesSqlAlchemy2OrmModules:
    @pytest.mark.parametrize("dag_path", _dag_files(), ids=lambda p: p.name)
    def test_dag_file_itself_has_no_banned_imports(self, dag_path: Path) -> None:
        tree = ast.parse(dag_path.read_text(encoding="utf-8"))
        offenders = _find_banned_imports(tree)
        assert offenders == [], (
            f"{dag_path.name} imports SQLAlchemy-2-only ORM modules: {offenders} "
            "-- these will raise ImportError in the packaged Airflow image "
            "(SQLAlchemy 1.4.51 pinned)."
        )

    @pytest.mark.parametrize("dag_path", _dag_files(), ids=lambda p: p.name)
    def test_scripts_imported_by_dag_have_no_banned_imports(self, dag_path: Path) -> None:
        tree = ast.parse(dag_path.read_text(encoding="utf-8"))
        script_modules = _find_scripts_imports(tree)

        checked = 0
        for module_name in sorted(script_modules):
            script_path = _script_module_path(module_name)
            if not script_path.is_file():
                continue  # not a plain scripts/<name>.py module; skip
            script_tree = ast.parse(script_path.read_text(encoding="utf-8"))
            offenders = _find_banned_imports(script_tree)
            checked += 1
            assert offenders == [], (
                f"{dag_path.name} imports {module_name}, which imports "
                f"SQLAlchemy-2-only ORM modules: {offenders} -- these will "
                f"raise ImportError the moment a task in {dag_path.name} "
                "actually calls into it inside the packaged Airflow image "
                "(SQLAlchemy 1.4.51 pinned), even though the import is lazy "
                "(inside a function) and every test here runs SQLAlchemy 2.x."
            )
        # Sanity: every DAG we know imports at least one scripts.* module
        # should actually have been checked (guards against the resolver
        # silently matching zero files if scripts/ layout ever changes).
        if dag_path.name == "daily_paper_trading.py":
            assert checked > 0, "expected daily_paper_trading.py to import scripts.* modules"
