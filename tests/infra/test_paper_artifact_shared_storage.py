"""Shared paper-trading artifact storage test for BUG-003 (Gate 01A, Phase 3).

Proves the bind mount declared in docker-compose.yml
(`${RQIS_PAPER_ARTIFACT_HOST_DIR}` -> `/opt/airflow/rqis_paper`, identical
in-container path on every Airflow service) actually round-trips bytes
between an Airflow container and the host filesystem surface the host-side
Streamlit dashboard/approval tooling reads directly (there is no Compose
dashboard service -- see docs/streamlit_dashboard_spec.md and
docs/runbooks/).

Skipped (not failed) when the `docker` CLI is unavailable. Uses a throwaway
temp directory as the host source (never touches the operator's real
local/paper_artifacts/), and never connects to a broker or database.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")

_CONTAINER_ARTIFACT_DIR = "/opt/airflow/rqis_paper"
_IMAGE = "python:3.11-slim"  # matches the Airflow image's Python minor version


def _docker_available() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.mark.integration
def test_container_write_is_readable_on_host_with_matching_sha256(tmp_path):
    if not _docker_available():
        pytest.skip("Docker daemon not reachable (docker info failed)")

    host_dir = tmp_path / "paper_artifacts"
    host_dir.mkdir()
    run_id = "01a_phase3_smoke_run"
    sentinel_bytes = b"rqis-01a-shared-artifact-sentinel\n"

    # Write from "inside a container" mounted exactly like an Airflow service
    # (uid 50000 is the apache/airflow image's non-root `airflow` user; using
    # a plain python image here keeps this test independent of the Airflow
    # image build so it can run even if Phase 2's image build is skipped).
    write_cmd = [
        "docker", "run", "--rm",
        "--user", "50000:0",
        "-v", f"{host_dir}:{_CONTAINER_ARTIFACT_DIR}",
        _IMAGE,
        "python", "-c",
        (
            f"import pathlib; "
            f"d = pathlib.Path('{_CONTAINER_ARTIFACT_DIR}') / '{run_id}'; "
            f"d.mkdir(parents=True, exist_ok=True); "
            f"(d / 'sentinel.txt').write_bytes({sentinel_bytes!r})"
        ),
    ]
    write = subprocess.run(write_cmd, capture_output=True, text=True, timeout=60)
    assert write.returncode == 0, f"container sentinel write failed: {write.stdout}\n{write.stderr}"

    host_sentinel = host_dir / run_id / "sentinel.txt"
    assert host_sentinel.is_file(), (
        f"host path {host_sentinel} does not exist after container write -- "
        "bind mount did not round-trip (BUG-003)"
    )
    host_bytes = host_sentinel.read_bytes()
    assert host_bytes == sentinel_bytes

    container_sha = hashlib.sha256(sentinel_bytes).hexdigest()
    host_sha = hashlib.sha256(host_bytes).hexdigest()
    assert container_sha == host_sha, "SHA-256 mismatch between container write and host read (BUG-003)"

    # Read back from a second, independent container invocation (simulating
    # the approval/dashboard surface reading what Airflow wrote), same path.
    read_cmd = [
        "docker", "run", "--rm",
        "-v", f"{host_dir}:{_CONTAINER_ARTIFACT_DIR}:ro",
        _IMAGE,
        "python", "-c",
        (
            f"import hashlib, pathlib, sys; "
            f"p = pathlib.Path('{_CONTAINER_ARTIFACT_DIR}') / '{run_id}' / 'sentinel.txt'; "
            f"sys.stdout.write(hashlib.sha256(p.read_bytes()).hexdigest())"
        ),
    ]
    read = subprocess.run(read_cmd, capture_output=True, text=True, timeout=60)
    assert read.returncode == 0, f"second-container sentinel read failed: {read.stdout}\n{read.stderr}"
    assert read.stdout.strip() == container_sha


class TestComposeArtifactMountContract:
    """Static YAML checks -- no Docker required."""

    def test_every_airflow_service_mounts_identical_artifact_path(self):
        import yaml

        repo_root = Path(__file__).resolve().parents[2]
        doc = yaml.safe_load((repo_root / "docker-compose.yml").read_text(encoding="utf-8"))
        for service in ("airflow-init", "airflow-webserver", "airflow-scheduler"):
            volumes = doc["services"][service]["volumes"]
            matches = [v for v in volumes if isinstance(v, str) and v.endswith(f":{_CONTAINER_ARTIFACT_DIR}")]
            assert matches, (
                f"{service} has no bind mount targeting {_CONTAINER_ARTIFACT_DIR} "
                "(BUG-003 requires the identical in-container path on every "
                "Airflow service)"
            )

    def test_artifact_mount_source_is_env_configurable_host_path(self):
        import yaml

        repo_root = Path(__file__).resolve().parents[2]
        doc = yaml.safe_load((repo_root / "docker-compose.yml").read_text(encoding="utf-8"))
        volumes = doc["services"]["airflow-scheduler"]["volumes"]
        matching = [v for v in volumes if isinstance(v, str) and v.endswith(f":{_CONTAINER_ARTIFACT_DIR}")]
        assert matching
        assert "RQIS_PAPER_ARTIFACT_HOST_DIR" in matching[0], (
            "artifact bind mount source should be operator-configurable via "
            "RQIS_PAPER_ARTIFACT_HOST_DIR, not a hardcoded host path"
        )
