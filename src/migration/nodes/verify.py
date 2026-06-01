"""verify node — apply patch on host, run tests inside sandbox, route result.

Isolation model (Phase 3):
  - git apply / git reset run on HOST (working tree is the sandbox mount point)
  - Test commands run INSIDE the sandbox (Docker container or E2B)
  - If SANDBOX_BACKEND=local, both run via subprocess (dev/test fallback)
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from agent_core.sandbox import SandboxResult, get_sandbox
from migration.profiles import load_profile
from migration.state import MigrationState, TestResult

log = logging.getLogger(__name__)

MAX_FIX_ATTEMPTS = int(os.environ.get("MAX_FIX_ATTEMPTS", "3"))


def verify(state: MigrationState) -> MigrationState:
    """Apply the patch and run tests in the sandbox.

    1. Reset any previous failed attempt (git checkout HEAD -- <file>)
    2. Apply the patch (git apply on host)
    3. Run the profile's test command inside the sandbox
    4. Parse pass/fail + failure lines for the fix node
    """
    repo_path = Path(state.get("repo_path", ""))
    patch = state.get("patch", "")
    profile_name = state.get("profile_name", "")
    fix_attempts = state.get("fix_attempts", 0)
    current_file = state.get("current_file", {})
    file_path = current_file.get("path", "")

    if not patch.strip():
        log.info("Empty patch — skipping apply, marking as pass")
        return {
            **state,
            "test_result": TestResult(success=True, output="(empty patch)", failures=[]),
        }

    # Reset the file if this is a retry (previous git apply left it dirty)
    if fix_attempts > 0:
        _git_reset_file(repo_path, file_path)

    # Apply patch on the host (also visible inside container via volume mount)
    apply_result = _apply_patch(repo_path, patch)
    if not apply_result["success"]:
        return {
            **state,
            "test_result": TestResult(
                success=False,
                output=apply_result["output"],
                failures=[f"patch apply failed:\n{apply_result['output']}"],
            ),
        }

    # Run tests inside the sandbox
    profile = load_profile(profile_name)
    log.info("Running tests in sandbox: %s", profile.test_command)
    sandbox_result = _run_in_sandbox(
        state=state,
        command=profile.test_command,
        cwd=str(repo_path),        # for LocalSandbox
        container_workdir="/workspace",  # for Docker/E2B
    )

    failures = _parse_failures(sandbox_result.combined_output)
    log.info(
        "Tests %s (exit=%d)",
        "PASSED" if sandbox_result.success else "FAILED",
        sandbox_result.exit_code,
    )

    return {
        **state,
        "test_result": TestResult(
            success=sandbox_result.success,
            output=sandbox_result.combined_output,
            failures=failures,
        ),
    }


def route_after_verify(state: MigrationState) -> str:
    """Conditional edge function: decide next node after verify."""
    result = state.get("test_result")
    if result and result.get("success"):
        return "pass"
    fix_attempts = state.get("fix_attempts", 0)
    if fix_attempts >= MAX_FIX_ATTEMPTS:
        return "give_up"
    return "fix"


def mark_give_up(state: MigrationState) -> MigrationState:
    """Called by the graph when route_after_verify returns 'give_up'.

    Sets current_file_gave_up=True so critic knows to raise HITL gate 2.
    Implemented as a thin wrapper — the graph routes give_up → this → critic.
    """
    return {**state, "current_file_gave_up": True}


# ── sandbox helpers ────────────────────────────────────────────────────────────

def _run_in_sandbox(
    state: MigrationState,
    command: str,
    cwd: str,
    container_workdir: str,
) -> SandboxResult:
    """Get (or reconnect to) the sandbox and run the test command."""
    backend = os.environ.get("SANDBOX_BACKEND", "local").lower()
    profile = load_profile(state.get("profile_name", ""))

    if backend == "docker":
        from agent_core.sandbox import DockerSandbox
        sandbox = DockerSandbox(
            image=profile.sandbox_image,
            repo_path=state.get("repo_path", cwd),
            container_id=state.get("sandbox_container_id") or None,
        )
        # Test command runs inside container; workdir = mount point
        result = sandbox.run(command, cwd=container_workdir)
        return result

    if backend == "e2b":
        from agent_core.sandbox import E2BSandbox
        sandbox = E2BSandbox(
            sandbox_id=state.get("e2b_sandbox_id") or None,
            template=profile.e2b_template,
        )
        result = sandbox.run(command)
        return result

    # LocalSandbox: subprocess on host
    sandbox = get_sandbox()
    return sandbox.run(command, cwd=cwd)


# ── git helpers ────────────────────────────────────────────────────────────────

def _apply_patch(repo_path: Path, patch: str) -> dict:
    patch_file = repo_path / ".agent_patch.diff"
    patch_file.write_text(patch)
    try:
        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file)],
            capture_output=True,
            text=True,
            cwd=repo_path,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            log.warning("git apply failed:\n%s", output)
        return {"success": result.returncode == 0, "output": output}
    finally:
        patch_file.unlink(missing_ok=True)


def _git_reset_file(repo_path: Path, file_path: str) -> None:
    if not file_path:
        return
    subprocess.run(
        ["git", "checkout", "HEAD", "--", file_path],
        capture_output=True,
        cwd=repo_path,
    )


# ── failure parsing ────────────────────────────────────────────────────────────

def _parse_failures(output: str) -> list[str]:
    """Extract structured failure messages from JUnit/Gradle/pytest/Kotlin output."""
    import re

    failures: list[str] = []

    # Gradle / JUnit: FAILED, ERROR, BUILD FAILED, compilation errors
    gradle_re = re.compile(
        r"\b(FAILED|BUILD FAILED|FAILURE|error:|CompilationError|Exception in thread)\b",
        re.I,
    )
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and gradle_re.search(stripped):
            failures.append(stripped)

    # Kotlin compiler errors: "file.kt:12:5: error: ..."
    kotlin_re = re.compile(r"^.+\.kt:\d+:\d+: error:.+", re.M)
    for m in kotlin_re.finditer(output):
        failures.append(m.group(0).strip())

    # pytest: "FAILED tests/foo.py::test_bar"
    pytest_re = re.compile(r"^(FAILED|ERROR)\s+\S+", re.M)
    for m in pytest_re.finditer(output):
        failures.append(m.group(0).strip())

    # Deduplicate, preserve order, cap
    seen: set[str] = set()
    result: list[str] = []
    for f in failures:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result[:25]
