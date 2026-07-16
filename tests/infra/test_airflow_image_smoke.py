"""Airflow image build + import smoke test for BUG-002 (Gate 01A, Phase 2).

Builds infra/docker/Dockerfile.airflow and runs
infra/docker/smoke_test_dag_imports.py inside the freshly built image with
the same volume/PYTHONPATH contract docker-compose.yml uses for the Airflow
services. This proves the image can import daily_paper_trading and every
module its tasks reach up to (and slightly past) the C1 approval gate,
without ModuleNotFoundError or a Python-version incompatibility.

Skipped (not failed) when the `docker` CLI is unavailable in the current
execution environment -- this suite is opportunistic evidence, not a
substitute for the operator running it directly per
docs/runbooks/airflow_fire_drill.md.

Never connects to a broker, database, or the actual Airflow scheduler/
webserver: only `docker build` and a single `docker run --entrypoint python`
invocation of a pure-import smoke script.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "infra" / "docker" / "Dockerfile.airflow"
_SMOKE_SCRIPT = _REPO_ROOT / "infra" / "docker" / "smoke_test_dag_imports.py"
_IMAGE_TAG = "rqis-airflow-01a-smoke:test"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.mark.slow
@pytest.mark.integration
def test_airflow_image_builds_and_imports_dag_modules(tmp_path):
    if not _docker_available():
        pytest.skip("Docker daemon not reachable (docker info failed)")

    build = subprocess.run(
        [
            "docker", "build",
            "-f", str(_DOCKERFILE),
            "-t", _IMAGE_TAG,
            str(_REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, (
        "docker build failed for infra/docker/Dockerfile.airflow "
        f"(BUG-002 gate):\n{build.stdout[-4000:]}\n{build.stderr[-4000:]}"
    )

    run_cmd = [
        "docker", "run", "--rm",
        "-e", "PYTHONPATH=/opt/airflow/rqis",
        "-e", "RQIS_RUNTIME_CONTEXT=compose_bridged",
        "-e", "PAPER_TRADING=true",
        "-e", "IBKR_PORT=7497",
        "-e", "IBKR_HOST=host.docker.internal",
        "-e", "RQIS_PAPER_ARTIFACT_DIR=/opt/airflow/rqis_paper",
        "-v", f"{_REPO_ROOT / 'airflow' / 'dags'}:/opt/airflow/dags",
        "-v", f"{_REPO_ROOT / 'airflow' / 'plugins'}:/opt/airflow/plugins",
        "-v", f"{_REPO_ROOT / 'data'}:/opt/airflow/rqis/data:ro",
        "-v", f"{_REPO_ROOT / 'signals'}:/opt/airflow/rqis/signals:ro",
        "-v", f"{_REPO_ROOT / 'config'}:/opt/airflow/rqis/config:ro",
        "-v", f"{_REPO_ROOT / 'scripts'}:/opt/airflow/rqis/scripts:ro",
        "-v", f"{_REPO_ROOT / 'execution'}:/opt/airflow/rqis/execution:ro",
        "-v", f"{_REPO_ROOT / 'risk'}:/opt/airflow/rqis/risk:ro",
        "-v", f"{_REPO_ROOT / 'portfolio'}:/opt/airflow/rqis/portfolio:ro",
        "-v", f"{_REPO_ROOT / 'reporting'}:/opt/airflow/rqis/reporting:ro",
        "-v", f"{_REPO_ROOT / 'backtesting'}:/opt/airflow/rqis/backtesting:ro",
        "-v", f"{_SMOKE_SCRIPT}:/opt/airflow/smoke_test_dag_imports.py:ro",
        "--entrypoint", "python",
        _IMAGE_TAG, "/opt/airflow/smoke_test_dag_imports.py",
    ]
    run = subprocess.run(run_cmd, capture_output=True, text=True, timeout=120)
    print(run.stdout)
    print(run.stderr)
    assert run.returncode == 0, (
        "Container DAG-import smoke test failed (BUG-002 gate):\n"
        f"{run.stdout[-4000:]}\n{run.stderr[-4000:]}"
    )
    assert "SMOKE TEST PASSED" in run.stdout
    assert "site-packages" in run.stdout or "dist-packages" in run.stdout
