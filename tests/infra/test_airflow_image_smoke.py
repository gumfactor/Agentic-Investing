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
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "infra" / "docker" / "Dockerfile.airflow"
_SMOKE_SCRIPT = _REPO_ROOT / "infra" / "docker" / "smoke_test_dag_imports.py"
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"
_IMAGE_TAG = "rqis-airflow-01a-smoke:test"


def _compose_airflow_bind_mounts() -> list[tuple[Path, str]]:
    """Derive the smoke-run mount list from docker-compose.yml itself
    (adversarial fix round P2-3): the smoke container must be mounted the
    same way the real Airflow services are, so the two lists cannot drift.

    Returns (absolute_source, "target[:mode]") tuples for repo-relative bind
    mounts, skipping named volumes (e.g. airflow_logs) and the
    env-substituted artifact mount (the smoke test does not touch it and a
    raw `${...}` source is not runnable outside compose). Tuples, not joined
    strings, because Windows drive letters contain ':' and would corrupt
    naive string splitting.
    """
    doc = yaml.safe_load(_COMPOSE_FILE.read_text(encoding="utf-8"))
    volumes = doc["x-airflow-common"]["volumes"]
    mounts: list[tuple[Path, str]] = []
    for vol in volumes:
        if not isinstance(vol, str) or not vol.startswith("./"):
            continue  # named volume (airflow_logs) or env-substituted mount
        source, _, rest = vol.partition(":")
        abs_source = (_REPO_ROOT / source[2:]).resolve()
        mounts.append((abs_source, rest))
    return mounts

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

    # P2-3: mounts are DERIVED from docker-compose.yml x-airflow-common, not
    # hand-maintained -- the smoke container sees exactly what the Airflow
    # services see.
    run_cmd = [
        "docker", "run", "--rm",
        "-e", "PYTHONPATH=/opt/airflow/rqis",
        "-e", "RQIS_RUNTIME_CONTEXT=compose_bridged",
        "-e", "PAPER_TRADING=true",
        "-e", "IBKR_PORT=7497",
        "-e", "IBKR_HOST=host.docker.internal",
        "-e", "RQIS_PAPER_ARTIFACT_DIR=/opt/airflow/rqis_paper",
    ]
    for source, target in _compose_airflow_bind_mounts():
        run_cmd += ["-v", f"{source}:{target}"]
    run_cmd += [
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


class TestMountContractConsistency:
    """P2-3: keep the compose mount contract and the smoke run in lockstep."""

    def test_compose_provides_every_tree_the_smoke_script_imports(self):
        """Every project package the smoke script imports must be reachable
        through a docker-compose.yml x-airflow-common bind mount (either
        under the PYTHONPATH root /opt/airflow/rqis, or on the dags/plugins
        paths Airflow adds itself). Fails if someone removes a compose mount
        that the DAG runtime actually needs."""
        targets = {target.split(":")[0] for _, target in _compose_airflow_bind_mounts()}
        required_targets = {
            "/opt/airflow/dags",
            "/opt/airflow/plugins",
            "/opt/airflow/rqis/data",
            "/opt/airflow/rqis/signals",
            "/opt/airflow/rqis/config",
            "/opt/airflow/rqis/scripts",
            "/opt/airflow/rqis/execution",
            "/opt/airflow/rqis/risk",
            "/opt/airflow/rqis/portfolio",
            "/opt/airflow/rqis/reporting",
            "/opt/airflow/rqis/backtesting",
        }
        missing = required_targets - targets
        assert not missing, (
            "docker-compose.yml x-airflow-common no longer mounts paths the "
            f"DAG import smoke test depends on: {sorted(missing)}"
        )

    def test_derived_mount_sources_exist_in_repo(self):
        for source, _target in _compose_airflow_bind_mounts():
            assert source.exists(), f"compose bind-mount source missing on disk: {source}"
