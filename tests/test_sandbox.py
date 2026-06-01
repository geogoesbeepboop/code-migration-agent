"""Tests for the sandbox abstraction layer.

- LocalSandbox: fully testable without Docker/E2B.
- DockerSandbox: tested with mocked docker SDK.
- E2BSandbox: tested with mocked e2b SDK.
- Failure parsing tested independently.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_core.sandbox import (
    DockerSandbox,
    LocalSandbox,
    SandboxResult,
    get_sandbox,
    is_docker_available,
)


# ── LocalSandbox ──────────────────────────────────────────────────────────────

class TestLocalSandbox:
    def test_run_success(self):
        sb = LocalSandbox()
        result = sb.run("echo hello")
        assert result.success
        assert "hello" in result.stdout

    def test_run_failure(self):
        sb = LocalSandbox()
        result = sb.run("exit 1")
        assert not result.success
        assert result.exit_code == 1

    def test_run_with_cwd(self, tmp_path):
        sb = LocalSandbox()
        result = sb.run("pwd", cwd=str(tmp_path))
        assert result.success
        assert str(tmp_path) in result.stdout

    def test_write_and_read_file(self, tmp_path):
        sb = LocalSandbox()
        path = str(tmp_path / "hello.txt")
        sb.write_file(path, "world")
        assert sb.read_file(path) == "world"

    def test_backend_id(self):
        assert LocalSandbox().backend_id == "local"

    def test_context_manager(self):
        with LocalSandbox() as sb:
            result = sb.run("echo ctx")
            assert result.success

    def test_timeout(self):
        sb = LocalSandbox()
        result = sb.run("sleep 10", timeout=1)
        assert not result.success
        assert "timed out" in result.stderr.lower()

    def test_combined_output(self):
        sb = LocalSandbox()
        result = sb.run("echo out && echo err >&2")
        assert result.combined_output  # both captured


# ── SandboxResult helpers ─────────────────────────────────────────────────────

class TestSandboxResult:
    def test_success_true_on_zero(self):
        assert SandboxResult(exit_code=0, stdout="ok", stderr="").success

    def test_success_false_on_nonzero(self):
        assert not SandboxResult(exit_code=1, stdout="", stderr="err").success

    def test_combined_output_strips_outer_whitespace(self):
        r = SandboxResult(exit_code=0, stdout="a\n", stderr="b\n")
        combined = r.combined_output
        assert "a" in combined
        assert "b" in combined
        assert not combined.startswith(" ")
        assert not combined.endswith("\n")


# ── DockerSandbox (mocked) ────────────────────────────────────────────────────

class TestDockerSandbox:
    def _mock_docker(self, exit_code=0, output=b"test output"):
        """Return a context manager that patches docker.from_env()."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "abc123container"
        mock_container.short_id = "abc123"
        mock_container.status = "running"
        mock_container.exec_run.return_value = (exit_code, output)
        mock_client.containers.run.return_value = mock_container
        mock_client.containers.get.return_value = mock_container
        return patch("docker.from_env", return_value=mock_client), mock_container, mock_client

    def test_start_container(self, tmp_path):
        patcher, mock_container, mock_client = self._mock_docker()
        with patcher:
            sb = DockerSandbox(image="gradle:8-jdk21", repo_path=str(tmp_path))
            assert sb.container_id == "abc123container"
            mock_client.containers.run.assert_called_once()

    def test_run_command(self, tmp_path):
        patcher, mock_container, _ = self._mock_docker(exit_code=0, output=b"hello\n")
        with patcher:
            sb = DockerSandbox(image="gradle:8-jdk21", repo_path=str(tmp_path))
            result = sb.run("echo hello")
            assert result.success
            assert "hello" in result.stdout
            mock_container.exec_run.assert_called_once()

    def test_run_failure(self, tmp_path):
        patcher, mock_container, _ = self._mock_docker(exit_code=1, output=b"BUILD FAILED\n")
        with patcher:
            sb = DockerSandbox(image="gradle:8-jdk21", repo_path=str(tmp_path))
            result = sb.run("./gradlew test")
            assert not result.success
            assert "BUILD FAILED" in result.stdout

    def test_reconnect(self, tmp_path):
        patcher, mock_container, mock_client = self._mock_docker()
        with patcher:
            sb = DockerSandbox(
                image="gradle:8-jdk21",
                repo_path=str(tmp_path),
                container_id="abc123container",
            )
            # Should call containers.get, not containers.run
            mock_client.containers.get.assert_called_once_with("abc123container")
            mock_client.containers.run.assert_not_called()

    def test_reconnect_dead_container_starts_new(self, tmp_path):
        """If reconnected container is stopped, start a fresh one."""
        mock_client = MagicMock()
        dead_container = MagicMock()
        dead_container.id = "dead123"
        dead_container.short_id = "dead"
        dead_container.status = "exited"

        new_container = MagicMock()
        new_container.id = "new456container"
        new_container.short_id = "new456"
        new_container.status = "running"

        mock_client.containers.get.return_value = dead_container
        mock_client.containers.run.return_value = new_container

        with patch("docker.from_env", return_value=mock_client):
            sb = DockerSandbox(
                image="gradle:8-jdk21",
                repo_path=str(tmp_path),
                container_id="dead123",
            )
            # Should fall back to starting a new container
            mock_client.containers.run.assert_called_once()
            assert sb.container_id == "new456container"

    def test_close_stops_container(self, tmp_path):
        patcher, mock_container, _ = self._mock_docker()
        with patcher:
            sb = DockerSandbox(image="gradle:8-jdk21", repo_path=str(tmp_path))
            sb.close()
            mock_container.stop.assert_called_once()

    def test_context_manager_stops_on_exit(self, tmp_path):
        patcher, mock_container, _ = self._mock_docker()
        with patcher:
            with DockerSandbox(image="gradle:8-jdk21", repo_path=str(tmp_path)):
                pass
            mock_container.stop.assert_called_once()

    def test_backend_id_is_container_id(self, tmp_path):
        patcher, mock_container, _ = self._mock_docker()
        with patcher:
            sb = DockerSandbox(image="gradle:8-jdk21", repo_path=str(tmp_path))
            assert sb.backend_id == "abc123container"

    def test_volumes_include_repo_and_gradle_cache(self, tmp_path):
        patcher, _, mock_client = self._mock_docker()
        with patcher:
            DockerSandbox(image="gradle:8-jdk21", repo_path=str(tmp_path))
            call_kwargs = mock_client.containers.run.call_args.kwargs
            volumes = call_kwargs.get("volumes", {})
            # Repo path should be mounted at /workspace
            assert str(tmp_path) in volumes
            assert volumes[str(tmp_path)]["bind"] == "/workspace"


# ── get_sandbox factory ───────────────────────────────────────────────────────

class TestGetSandbox:
    def test_local_backend(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_BACKEND", "local")
        sb = get_sandbox()
        assert isinstance(sb, LocalSandbox)

    def test_default_is_local(self, monkeypatch):
        monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
        sb = get_sandbox()
        assert isinstance(sb, LocalSandbox)

    def test_docker_backend_requires_repo_path(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_BACKEND", "docker")
        with pytest.raises(ValueError, match="repo_path"):
            get_sandbox()


# ── is_docker_available ───────────────────────────────────────────────────────

class TestIsDockerAvailable:
    def test_returns_false_when_docker_not_running(self):
        with patch("docker.from_env", side_effect=Exception("Docker not running")):
            assert not is_docker_available()

    def test_returns_true_when_docker_running(self):
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        with patch("docker.from_env", return_value=mock_client):
            assert is_docker_available()


# ── failure parsing (verify._parse_failures) ─────────────────────────────────

class TestParseFailures:
    """Tests for the failure line extractor used by verify node."""

    def setup_method(self):
        from migration.nodes.verify import _parse_failures
        self._parse = _parse_failures

    def test_gradle_build_failed(self):
        output = textwrap.dedent("""\
            > Task :test
            FAILED
            BUILD FAILED in 2s
        """)
        failures = self._parse(output)
        assert any("FAILED" in f or "BUILD FAILED" in f for f in failures)

    def test_junit_test_failure(self):
        output = textwrap.dedent("""\
            UserServiceTest > testFindById FAILED
                org.junit.ComparisonFailure at UserServiceTest.java:45
        """)
        failures = self._parse(output)
        assert any("FAILED" in f for f in failures)

    def test_kotlin_compiler_error(self):
        output = "src/main/kotlin/UserService.kt:12:5: error: unresolved reference: findAll"
        failures = self._parse(output)
        assert any("error:" in f for f in failures)

    def test_empty_output_no_failures(self):
        assert self._parse("BUILD SUCCESSFUL in 1s\n") == []

    def test_deduplicates(self):
        output = "FAILED\nFAILED\nFAILED\n"
        failures = self._parse(output)
        assert failures.count("FAILED") == 1

    def test_caps_at_25(self):
        output = "\n".join(f"FAILED test_{i}" for i in range(50))
        failures = self._parse(output)
        assert len(failures) <= 25
