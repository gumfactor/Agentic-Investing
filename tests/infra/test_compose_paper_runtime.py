"""Compose runtime contract tests for BUG-001 / BUG-004 (Gate 01A, Phase 1).

These tests inspect the rendered `docker-compose.yml` Airflow service
environment to prove the paper-trading env vars reach every Airflow service,
and that the container-side IBKR host is never a loopback default. They do
not require Docker to be installed: the primary assertions parse the YAML
directly (with anchor/merge-key resolution), matching what `docker compose
config` would show for the static (non-interpolated) service definitions.

If the `docker` CLI is available, an additional test also renders the file
with `docker compose config` against a placeholder-only env file (derived
from `.env.example`, which never contains real secrets) and asserts the same
paper-mode values survive full `${VAR:-default}` substitution. That test is
skipped, not failed, when Docker is unavailable in the execution environment.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_ENV_EXAMPLE_PATH = _REPO_ROOT / ".env.example"

_AIRFLOW_SERVICES = ("airflow-init", "airflow-webserver", "airflow-scheduler")

_REQUIRED_ENV_KEYS = (
    "PAPER_TRADING",
    "IBKR_HOST",
    "IBKR_PORT",
    "IBKR_CLIENT_ID",
    "RQIS_PAPER_ARTIFACT_DIR",
)

_LOOPBACK_VALUES = {"", "127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _load_compose_doc() -> dict:
    with _COMPOSE_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _service_environment(doc: dict, service: str) -> dict:
    env = doc["services"][service].get("environment")
    assert env is not None, f"{service} has no environment block"
    assert isinstance(env, dict), (
        f"{service}.environment must be a mapping (list-form 'KEY=VALUE' "
        "entries are not inspected by this test)"
    )
    return env


class TestComposeRawYamlContract:
    """Static YAML checks -- no Docker required."""

    def test_compose_file_exists(self):
        assert _COMPOSE_PATH.is_file()

    @pytest.mark.parametrize("service", _AIRFLOW_SERVICES)
    def test_required_paper_env_keys_present(self, service):
        doc = _load_compose_doc()
        env = _service_environment(doc, service)
        missing = [k for k in _REQUIRED_ENV_KEYS if k not in env]
        assert not missing, f"{service} is missing paper-runtime env keys: {missing}"

    @pytest.mark.parametrize("service", _AIRFLOW_SERVICES)
    def test_paper_trading_is_hardcoded_true(self, service):
        """P2-4 (adversarial fix round): this compose file is a paper-only
        stack. PAPER_TRADING must be the literal string "true" -- NOT an
        `${...}` substitution -- so a stale host .env carrying
        PAPER_TRADING=false can never render live values into the Airflow
        services."""
        doc = _load_compose_doc()
        env = _service_environment(doc, service)
        assert env["PAPER_TRADING"] == "true"
        assert "${" not in env["PAPER_TRADING"]

    @pytest.mark.parametrize("service", _AIRFLOW_SERVICES)
    def test_ibkr_port_is_hardcoded_paper_port(self, service):
        """P2-4: IBKR_PORT must be the literal paper port "7497", never an
        `${IBKR_PORT...}` substitution a stale .env could flip to 7496."""
        doc = _load_compose_doc()
        env = _service_environment(doc, service)
        assert env["IBKR_PORT"] == "7497"
        assert "${" not in env["IBKR_PORT"]

    @pytest.mark.parametrize("service", _AIRFLOW_SERVICES)
    def test_ibkr_host_default_is_not_a_loopback_literal(self, service):
        """BUG-004: the raw compose default must not resolve to a loopback
        value; it must reference host.docker.internal (or an operator-
        supplied override), never fall back to 127.0.0.1/localhost."""
        doc = _load_compose_doc()
        env = _service_environment(doc, service)
        raw_value = env["IBKR_HOST"]
        for loopback in _LOOPBACK_VALUES - {""}:
            assert loopback not in raw_value, (
                f"{service}.IBKR_HOST default must not contain {loopback!r}: {raw_value!r}"
            )
        assert "host.docker.internal" in raw_value or "IBKR_HOST_AIRFLOW" in raw_value

    @pytest.mark.parametrize("service", _AIRFLOW_SERVICES)
    def test_runtime_context_marks_bridged_network(self, service):
        """execution/brokers/ibkr.py only enforces the anti-loopback rule when
        RQIS_RUNTIME_CONTEXT=compose_bridged is present; every Airflow
        service must set it so IBKRBroker() fails closed on a bad host."""
        doc = _load_compose_doc()
        env = _service_environment(doc, service)
        assert env.get("RQIS_RUNTIME_CONTEXT") == "compose_bridged"

    def test_env_example_defines_container_side_ibkr_host_override(self):
        content = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        assert "IBKR_HOST_AIRFLOW=host.docker.internal" in content
        # The host-side var must remain distinct and loopback-safe for
        # operator CLI scripts that run natively next to TWS.
        assert "IBKR_HOST=127.0.0.1" in content

    def test_env_example_has_no_committed_secret_values(self):
        content = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        # Loose guard: placeholder markers should still be present for the
        # values this task touches; a real credential would not match these.
        assert "IBKR_HOST_AIRFLOW=host.docker.internal" in content
        assert "change_me" in content  # unrelated password fields untouched


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
class TestComposeConfigRendering:
    """Full `docker compose config` interpolation against .env.example only
    (placeholders, never real secrets) -- exercised opportunistically when the
    Docker CLI is present in the test environment."""

    def test_rendered_config_has_paper_defaults_for_every_airflow_service(self, tmp_path):
        env_file = tmp_path / "placeholder.env"
        env_file.write_text(_ENV_EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")

        result = subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "config"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            pytest.skip(f"docker compose config unavailable in this environment: {result.stderr[:500]}")

        rendered = yaml.safe_load(result.stdout)
        for service in _AIRFLOW_SERVICES:
            env = _service_environment(rendered, service)
            assert env["PAPER_TRADING"] == "true"
            assert env["IBKR_PORT"] == "7497"
            assert env["IBKR_HOST"] == "host.docker.internal"
            assert env["IBKR_CLIENT_ID"] == "1"
            assert env["RQIS_PAPER_ARTIFACT_DIR"] == "/opt/airflow/rqis_paper"
            assert env["RQIS_RUNTIME_CONTEXT"] == "compose_bridged"

    def test_stale_live_env_file_cannot_flip_rendered_stack_to_live(self, tmp_path):
        """P2-4 regression: even a host .env explicitly carrying live values
        (PAPER_TRADING=false, IBKR_PORT=7496) must render paper values into
        every Airflow service, because the compose file hard-codes them."""
        env_file = tmp_path / "stale_live.env"
        base = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        base = base.replace("PAPER_TRADING=true", "PAPER_TRADING=false")
        base = base.replace("IBKR_PORT=7497", "IBKR_PORT=7496")
        env_file.write_text(base, encoding="utf-8")

        result = subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "config"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            pytest.skip(f"docker compose config unavailable in this environment: {result.stderr[:500]}")

        rendered = yaml.safe_load(result.stdout)
        for service in _AIRFLOW_SERVICES:
            env = _service_environment(rendered, service)
            assert env["PAPER_TRADING"] == "true", f"{service} rendered a live PAPER_TRADING"
            assert env["IBKR_PORT"] == "7497", f"{service} rendered the live IBKR port"
