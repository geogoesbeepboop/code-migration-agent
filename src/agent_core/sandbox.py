"""Sandbox Protocol — isolates all generated code execution.

Backends (selected via SANDBOX_BACKEND env var):
  local   — LocalSandbox: subprocess on the host (dev/test only, no isolation)
  docker  — DockerSandbox: Docker container with repo volume-mounted (local dev)
  e2b     — E2BSandbox: E2B cloud sandbox (CI, requires E2B_API_KEY)

All backends share the same Protocol so nodes are backend-agnostic.

Key design:
  - DockerSandbox/E2BSandbox run test commands in an isolated environment.
  - Patch application (git apply) and commits still happen on the host, because
    the repo is volume-mounted. Files changed on the host are immediately
    visible inside the container.
  - Containers persist for the lifetime of a migration run via container_id /
    sandbox_id stored in LangGraph state.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shlex
import subprocess
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

# ── shared result type ─────────────────────────────────────────────────────────

@dataclasses.dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def combined_output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


# ── Protocol ───────────────────────────────────────────────────────────────────

@runtime_checkable
class Sandbox(Protocol):
    """Minimal interface for all sandbox backends."""

    def run(self, command: str, cwd: str | None = None,
            timeout: int | None = None) -> SandboxResult: ...

    def write_file(self, path: str, content: str) -> None: ...
    def read_file(self, path: str) -> str: ...

    def close(self) -> None: ...

    @property
    def backend_id(self) -> str:
        """Opaque ID for reconnecting to this sandbox from serialised state."""
        ...

    def __enter__(self) -> "Sandbox": ...
    def __exit__(self, *_) -> None: ...


# ── LocalSandbox ──────────────────────────────────────────────────────────────

class LocalSandbox:
    """Runs commands in a local subprocess.  Dev / test fallback — no isolation."""

    backend = "local"

    def run(self, command: str, cwd: str | None = None,
            timeout: int | None = None) -> SandboxResult:
        timeout = timeout or int(os.environ.get("TEST_TIMEOUT_SECONDS", "300"))
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout,
            )
            return SandboxResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                exit_code=1,
                stdout="",
                stderr=f"Command timed out after {timeout}s: {command}",
            )

    def write_file(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def read_file(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def close(self) -> None:
        pass

    @property
    def backend_id(self) -> str:
        return "local"

    def __enter__(self) -> "LocalSandbox":
        return self

    def __exit__(self, *_) -> None:
        self.close()


# ── DockerSandbox ─────────────────────────────────────────────────────────────

class DockerSandbox:
    """Runs test commands inside a Docker container.

    The repo is volume-mounted at workdir (default /workspace) so:
    - Files patched on the host are immediately visible inside the container.
    - git operations (apply, commit) run on the host, not the container.
    - Only the test execution is sandboxed.

    Resource limits (mem, cpu) and network isolation are applied by default.
    The container persists across nodes via container_id stored in state.
    """

    backend = "docker"

    _DEFAULT_WORKDIR = "/workspace"
    _DEFAULT_MEM_LIMIT = os.environ.get("SANDBOX_MEM_LIMIT", "4g")
    # CPU quota: 50000 / 100000 = 0.5 CPUs.  Override with SANDBOX_CPU_QUOTA.
    _DEFAULT_CPU_QUOTA = int(os.environ.get("SANDBOX_CPU_QUOTA", "100000"))

    def __init__(
        self,
        image: str,
        repo_path: str,
        *,
        workdir: str = _DEFAULT_WORKDIR,
        extra_volumes: dict | None = None,
        container_id: str | None = None,
        network_mode: str = "bridge",
        mem_limit: str = _DEFAULT_MEM_LIMIT,
        cpu_quota: int = _DEFAULT_CPU_QUOTA,
    ) -> None:
        import docker  # lazy import

        self._client = docker.from_env()
        self._workdir = workdir
        self._image = image
        self._repo_path = os.path.abspath(repo_path)

        if container_id:
            self._container = self._reconnect(container_id)
        else:
            self._container = self._start(
                image=image,
                repo_path=self._repo_path,
                workdir=workdir,
                extra_volumes=extra_volumes or {},
                network_mode=network_mode,
                mem_limit=mem_limit,
                cpu_quota=cpu_quota,
            )
            log.info("DockerSandbox started: %s  image=%s", self._container.short_id, image)

    # ── public API ─────────────────────────────────────────────────────────────

    def run(self, command: str, cwd: str | None = None,
            timeout: int | None = None) -> SandboxResult:
        cwd = cwd or self._workdir
        timeout = timeout or int(os.environ.get("TEST_TIMEOUT_SECONDS", "300"))
        self._container.reload()
        if self._container.status != "running":
            raise RuntimeError(
                f"DockerSandbox container {self._container.short_id} is not running "
                f"(status={self._container.status})"
            )
        exit_code, output = self._container.exec_run(
            cmd=["sh", "-c", command],
            workdir=cwd,
            demux=False,
        )
        decoded = output.decode(errors="replace") if output else ""
        return SandboxResult(exit_code=exit_code, stdout=decoded, stderr="")

    def write_file(self, path: str, content: str) -> None:
        """Write a file inside the container using tar injection."""
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            data = content.encode()
            info = tarfile.TarInfo(name=os.path.basename(path))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        self._container.put_archive(os.path.dirname(path) or "/", buf)

    def read_file(self, path: str) -> str:
        result = self._container.exec_run(f"cat {shlex.quote(path)}")
        return result.output.decode(errors="replace")

    def close(self) -> None:
        try:
            self._container.stop(timeout=5)
            log.info("DockerSandbox stopped: %s", self._container.short_id)
        except Exception as e:
            log.warning("Error stopping container %s: %s", self._container.short_id, e)

    @property
    def backend_id(self) -> str:
        return self._container.id

    @property
    def container_id(self) -> str:
        return self._container.id

    def __enter__(self) -> "DockerSandbox":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── internals ──────────────────────────────────────────────────────────────

    def _start(
        self,
        image: str,
        repo_path: str,
        workdir: str,
        extra_volumes: dict,
        network_mode: str,
        mem_limit: str,
        cpu_quota: int,
    ):
        import docker

        # Gradle cache volume — avoids re-downloading dependencies on every run
        gradle_cache = os.path.expanduser(
            os.environ.get("GRADLE_USER_HOME", "~/.gradle")
        )
        os.makedirs(gradle_cache, exist_ok=True)

        volumes = {
            repo_path: {"bind": workdir, "mode": "rw"},
            gradle_cache: {"bind": "/root/.gradle", "mode": "rw"},
            **extra_volumes,
        }

        try:
            return self._client.containers.run(
                image,
                command="sleep infinity",
                detach=True,
                remove=True,
                volumes=volumes,
                working_dir=workdir,
                network_mode=network_mode,
                mem_limit=mem_limit,
                cpu_period=100_000,
                cpu_quota=cpu_quota,
            )
        except docker.errors.ImageNotFound:
            log.info("Pulling Docker image: %s", image)
            self._client.images.pull(image)
            return self._client.containers.run(
                image,
                command="sleep infinity",
                detach=True,
                remove=True,
                volumes=volumes,
                working_dir=workdir,
                network_mode=network_mode,
                mem_limit=mem_limit,
                cpu_period=100_000,
                cpu_quota=cpu_quota,
            )

    def _reconnect(self, container_id: str):
        try:
            container = self._client.containers.get(container_id)
            container.reload()
            if container.status in ("running", "paused"):
                log.debug("Reconnected to container %s", container.short_id)
                return container
            log.warning(
                "Container %s is not running (status=%s); starting a new one",
                container.short_id,
                container.status,
            )
        except Exception as e:
            log.warning("Could not reconnect to container %s: %s", container_id, e)

        # Fall back: start a fresh container with same image/repo
        return self._start(
            image=self._image,
            repo_path=self._repo_path,
            workdir=self._workdir,
            extra_volumes={},
            network_mode="bridge",
            mem_limit=self._DEFAULT_MEM_LIMIT,
            cpu_quota=self._DEFAULT_CPU_QUOTA,
        )


# ── E2BSandbox ────────────────────────────────────────────────────────────────

class E2BSandbox:
    """Runs commands in an E2B cloud sandbox (CI environments).

    Requires: E2B_API_KEY environment variable.
    The full repo is uploaded on first use; subsequent calls reuse the sandbox.

    E2B docs: https://e2b.dev/docs
    """

    backend = "e2b"

    def __init__(
        self,
        template: str = "base",
        repo_path: str | None = None,
        *,
        sandbox_id: str | None = None,
        timeout_seconds: int = 3600,
    ) -> None:
        from e2b import Sandbox as _E2B  # lazy import

        self._template = template
        self._repo_path = repo_path
        self._remote_workdir = "/home/user/repo"

        if sandbox_id:
            # Reconnect to existing sandbox: pass sandbox_id via opts
            self._sb = _E2B(sandbox_id=sandbox_id)
            self._sb.connect(timeout=timeout_seconds)
            log.info("E2BSandbox reconnected: %s", sandbox_id)
        else:
            self._sb = _E2B.create(template=template, timeout=timeout_seconds)
            if repo_path:
                self._upload_repo(repo_path)
            log.info("E2BSandbox created: %s", self._sb.sandbox_id)

    # ── public API ─────────────────────────────────────────────────────────────

    def run(self, command: str, cwd: str | None = None,
            timeout: int | None = None) -> SandboxResult:
        cwd = cwd or self._remote_workdir
        timeout = timeout or int(os.environ.get("TEST_TIMEOUT_SECONDS", "300"))
        result = self._sb.commands.run(command, cwd=cwd, timeout=float(timeout))
        return SandboxResult(
            exit_code=result.exit_code,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    def write_file(self, path: str, content: str) -> None:
        self._sb.files.write(path, content)

    def read_file(self, path: str) -> str:
        return self._sb.files.read(path, format="text")

    def close(self) -> None:
        try:
            self._sb.kill()
            log.info("E2BSandbox killed: %s", self._sb.sandbox_id)
        except Exception as e:
            log.warning("Error killing E2B sandbox: %s", e)

    @property
    def backend_id(self) -> str:
        return self._sb.sandbox_id

    def __enter__(self) -> "E2BSandbox":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── internals ──────────────────────────────────────────────────────────────

    def _upload_repo(self, repo_path: str) -> None:
        """Upload the repo to the E2B sandbox as a tarball."""
        import io
        import tarfile

        log.info("Uploading repo to E2B sandbox: %s", repo_path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(repo_path, arcname="repo")
        buf.seek(0)

        self._sb.files.write("/tmp/repo.tar.gz", buf.getvalue())
        self._sb.commands.run(
            f"mkdir -p {self._remote_workdir} && "
            f"tar -xzf /tmp/repo.tar.gz -C /home/user --strip-components=1"
        )


# ── factory ───────────────────────────────────────────────────────────────────

def get_sandbox(
    repo_path: str | None = None,
    image: str = "gradle:8-jdk21",
    *,
    container_id: str | None = None,
    sandbox_id: str | None = None,
) -> Sandbox:
    """Create or reconnect to a sandbox based on SANDBOX_BACKEND env var."""
    backend = os.environ.get("SANDBOX_BACKEND", "local").lower()

    if backend == "docker":
        if not repo_path:
            raise ValueError("repo_path required for DockerSandbox")
        return DockerSandbox(
            image=image,
            repo_path=repo_path,
            container_id=container_id,
        )
    if backend == "e2b":
        return E2BSandbox(
            repo_path=repo_path,
            sandbox_id=sandbox_id,
        )

    return LocalSandbox()


def is_docker_available() -> bool:
    """Return True if Docker daemon is reachable."""
    try:
        import docker
        docker.from_env().ping()
        return True
    except Exception:
        return False
