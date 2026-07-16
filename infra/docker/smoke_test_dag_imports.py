"""Container smoke test for BUG-002 (Gate 01A, Phase 2).

Runs *inside* the built `rqis-airflow` image (not on the host) with the same
volumes and PYTHONPATH the Compose Airflow services use. It proves the image
can actually execute the paper-trading DAG's imports rather than failing at
task runtime with ModuleNotFoundError.

Usage (mirrors the docker-compose.yml x-airflow-common mount/env contract;
adjust <repo_root> to an absolute path):

    docker run --rm \
      -e PYTHONPATH=/opt/airflow/rqis \
      -e RQIS_RUNTIME_CONTEXT=compose_bridged \
      -e PAPER_TRADING=true -e IBKR_PORT=7497 -e IBKR_HOST=host.docker.internal \
      -e RQIS_PAPER_ARTIFACT_DIR=/opt/airflow/rqis_paper \
      -v <repo_root>/airflow/dags:/opt/airflow/dags \
      -v <repo_root>/airflow/plugins:/opt/airflow/plugins \
      -v <repo_root>/data:/opt/airflow/rqis/data:ro \
      -v <repo_root>/signals:/opt/airflow/rqis/signals:ro \
      -v <repo_root>/config:/opt/airflow/rqis/config:ro \
      -v <repo_root>/scripts:/opt/airflow/rqis/scripts:ro \
      -v <repo_root>/execution:/opt/airflow/rqis/execution:ro \
      -v <repo_root>/risk:/opt/airflow/rqis/risk:ro \
      -v <repo_root>/portfolio:/opt/airflow/rqis/portfolio:ro \
      -v <repo_root>/reporting:/opt/airflow/rqis/reporting:ro \
      -v <repo_root>/backtesting:/opt/airflow/rqis/backtesting:ro \
      -v <repo_root>/infra/docker/smoke_test_dag_imports.py:/opt/airflow/smoke_test_dag_imports.py:ro \
      --entrypoint python <image> /opt/airflow/smoke_test_dag_imports.py

Exit code 0 on success; non-zero (with a traceback for the failing import) on
any missing dependency or an unexpected `airflow` package resolution.

Never connects to a database, broker, or object store: every import listed
here is safe at module-import time (no I/O is triggered by importing).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Modules reached by daily_paper_trading.py's tasks, including everything
# through and slightly beyond the C1 approval gate (wait_approval), per the
# BUG-002 acceptance requirement ("every module needed before the approval
# gate"). Listed in DAG task order for readability.
_PRE_AND_POST_APPROVAL_MODULES = [
    # verify_inputs (Step 2)
    "scripts.paper_inputs_check",
    # construct_target (Step 3)
    "scripts.paper_target_check",
    # fetch_ibkr_snapshot (Step 3.5) -- broker class import only, no connect()
    "execution.brokers.ibkr",
    "execution.oms.order",
    "execution.brokers.base",
    # gen_candidates (Step 4)
    "scripts.paper_order_candidates_check",
    "backtesting.engine.fill_simulator",
    # risk_compliance_gate (Step 5)
    "scripts.paper_risk_compliance_check",
    "execution.oms.compliance",
    # build_blotter (Step 6)
    "scripts.paper_stage_blotter_check",
    # whatif_validate (Step 6.5, last task before the C1 approval sensor)
    "scripts.paper_whatif_check",
    # wait_approval (C1 gate) -- the sensor plugin itself
    "blotter_approval_sensor",
    # Beyond the approval gate, imported for completeness/coverage:
    "scripts.paper_submit_reconcile_check",
    "scripts.paper_order_reconcile_check",
    "scripts.paper_run_audit_check",
    "scripts.paper_operational_ledger_check",
    "reporting.audit.paper_operational_ledger",
]

_DAG_MODULES_ON_PYTHONPATH = ["daily_paper_trading", "daily_signal_pipeline", "daily_data_pipeline"]


def _fail(message: str) -> None:
    print(f"SMOKE TEST FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    import airflow  # noqa: PLC0415 -- intentionally imported here, not at module top

    airflow_file = getattr(airflow, "__file__", None) or ""
    print(f"airflow.__file__ = {airflow_file}")
    print(f"airflow version  = {airflow.__version__}")

    repo_stub_marker = str(Path("airflow") / "__init__.py")
    if "site-packages" not in airflow_file and "dist-packages" not in airflow_file:
        _fail(
            f"airflow.__file__={airflow_file!r} does not look like an installed "
            "package (expected a site-packages/dist-packages path). This "
            "container may be resolving this repository's local `airflow/` "
            "test-stub package instead of the base image's real Apache "
            f"Airflow install (repo stub marker: {repo_stub_marker})."
        )
    print("OK: airflow resolves to an installed package, not the repo's local test stub.")

    sys.path.insert(0, "/opt/airflow/dags")
    sys.path.insert(0, "/opt/airflow/plugins")

    failures: list[str] = []

    for mod_name in _DAG_MODULES_ON_PYTHONPATH:
        try:
            importlib.import_module(mod_name)
            print(f"OK: imported {mod_name}")
        except Exception as exc:  # noqa: BLE001 -- report every failure, not just the first
            failures.append(f"{mod_name}: {exc!r}")
            print(f"FAIL: {mod_name}: {exc!r}", file=sys.stderr)

    for mod_name in _PRE_AND_POST_APPROVAL_MODULES:
        try:
            importlib.import_module(mod_name)
            print(f"OK: imported {mod_name}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{mod_name}: {exc!r}")
            print(f"FAIL: {mod_name}: {exc!r}", file=sys.stderr)

    if failures:
        _fail(f"{len(failures)} module(s) failed to import:\n  " + "\n  ".join(failures))

    print(f"SMOKE TEST PASSED: {len(_DAG_MODULES_ON_PYTHONPATH) + len(_PRE_AND_POST_APPROVAL_MODULES)} modules imported cleanly.")


if __name__ == "__main__":
    main()
