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
- Round 10 self-audit (Codex/PM directive): the original version of this
  module only walked ONE level deep — the DAG file itself, plus whichever
  ``scripts.*`` modules the DAG imports DIRECTLY. It did not recurse into
  modules that those scripts import in turn (e.g.
  ``scripts/paper_stage_blotter_check.py`` imports
  ``scripts.paper_risk_compliance_check``, which imports
  ``scripts.paper_order_candidates_check``, etc. — a real multi-level
  import graph, see the paper_* family under ``scripts/``), nor did it
  check ``data.*`` modules reached indirectly through a script (only the
  banned modules imported directly by an already-visited file were caught).
  A script reachable ONLY transitively -- imported by another script but
  never directly by the DAG -- would have been silently unchecked. Widened
  to a full breadth-first closure over every ``scripts.*`` AND ``data.*``
  module transitively reachable from each DAG file, with no depth limit
  (bounded only by a visited-set to handle cycles), checking every visited
  module for banned imports.

This module is a repo-wide, generic version of the DAG-local
``TestImportIsolation`` check in ``tests/test_daily_signal_pipeline_pit.py``
(which only covers ``daily_signal_pipeline.py`` itself): it statically
walks every DAG file under ``airflow/dags/``, transitively resolves every
``scripts.*``/``data.*`` import reachable from it (module level or inside a
function — Airflow tasks lazily import their dependencies as a matter of
course), and asserts none of them import a banned module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DAGS_DIR = _REPO_ROOT / "airflow" / "dags"

_BANNED_MODULES = (
    "data.universe.runtime",
    "data.universe.models",
    "data.universe.import_pipeline",
    "data.research.identity",
    "data.research.models",
)
_BANNED_PACKAGES = ("data.universe", "data.research")

# Root packages whose imports we resolve to a source file and recurse into.
# Anything outside these (stdlib, third-party, airflow.*, signals.*,
# backtesting.*, ...) is out of scope for this specific SQLAlchemy-1.4
# compatibility concern and is not walked.
_WALKED_ROOTS = ("scripts", "data")


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


def _find_walked_imports(tree: ast.AST) -> set[str]:
    """Every ``scripts.*``/``data.*`` dotted module target imported anywhere
    in this file (module level or inside a function — Airflow tasks lazily
    import). Includes intermediate ancestors implicitly imported by a
    dotted ``import scripts.foo.bar`` form, matched via startswith at
    resolution time, so only leaf targets need to be collected here.
    """
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(_WALKED_ROOTS):
            targets.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_WALKED_ROOTS):
                    targets.add(alias.name)
    return targets


def _module_path(module_name: str) -> Path:
    # "scripts.paper_inputs_check" -> scripts/paper_inputs_check.py
    # "data.research.sql_compat"   -> data/research/sql_compat.py
    # "data.universe"              -> data/universe/__init__.py
    parts = module_name.split(".")
    file_path = _REPO_ROOT.joinpath(*parts).with_suffix(".py")
    if file_path.is_file():
        return file_path
    return _REPO_ROOT.joinpath(*parts) / "__init__.py"


def _transitive_closure(start_tree: ast.AST) -> dict[str, ast.AST]:
    """BFS over every scripts.*/data.* module transitively reachable from
    start_tree (typically a DAG file's own AST). Returns {module_name: ast}
    for every module actually visited (module_name -> its own parsed tree),
    so the caller can check each one for banned imports. A visited-set
    prevents infinite loops on import cycles.
    """
    visited: dict[str, ast.AST] = {}
    worklist: list[str] = sorted(_find_walked_imports(start_tree))
    seen: set[str] = set()

    while worklist:
        module_name = worklist.pop()
        if module_name in seen:
            continue
        seen.add(module_name)

        module_path = _module_path(module_name)
        if not module_path.is_file():
            continue  # not a resolvable first-party module; skip

        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        visited[module_name] = tree

        for discovered in _find_walked_imports(tree):
            if discovered not in seen:
                worklist.append(discovered)

    return visited


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
    def test_transitively_reachable_modules_have_no_banned_imports(self, dag_path: Path) -> None:
        """Full BFS closure, not just modules the DAG imports directly
        (round 10 self-audit widening — see module docstring)."""
        tree = ast.parse(dag_path.read_text(encoding="utf-8"))
        reachable = _transitive_closure(tree)

        for module_name, module_tree in sorted(reachable.items()):
            offenders = _find_banned_imports(module_tree)
            assert offenders == [], (
                f"{dag_path.name} transitively reaches {module_name}, which "
                f"imports SQLAlchemy-2-only ORM modules: {offenders} -- these "
                f"will raise ImportError the moment a task in {dag_path.name} "
                "actually calls into it inside the packaged Airflow image "
                "(SQLAlchemy 1.4.51 pinned), even though the import is lazy "
                "(inside a function) and every test here runs SQLAlchemy 2.x."
            )

        # Sanity: every DAG we know imports at least one scripts.*/data.*
        # module transitively should actually have visited something (guards
        # against the resolver silently matching zero files if the repo
        # layout ever changes).
        if dag_path.name == "daily_paper_trading.py":
            assert len(reachable) > 0, (
                "expected daily_paper_trading.py to transitively reach "
                "scripts.*/data.* modules"
            )
            # This DAG's own scripts.* import graph is multi-level (e.g.
            # paper_stage_blotter_check.py imports
            # paper_risk_compliance_check.py, which imports
            # paper_order_candidates_check.py) -- confirm the BFS actually
            # walked past depth 1, not just the DAG's own direct imports,
            # so a future regression narrowing this back to one level would
            # fail this sanity check rather than silently passing.
            directly_imported = _find_walked_imports(tree)
            assert reachable.keys() - directly_imported, (
                "expected at least one module reached only transitively "
                "(depth > 1), not just modules daily_paper_trading.py "
                "imports directly -- if this is empty, the BFS may have "
                "regressed to a depth-1 walk"
            )

        if dag_path.name == "daily_signal_pipeline.py":
            assert "data.normalization.corporate_actions" in reachable
            assert "data.universe.calendar" in reachable
