"""Test runner: applies a patch in the Sandbox and runs the profile's test command.

Phase 0: interface defined, implementation complete (thin wrapper over Sandbox).
Phase 3: full DockerSandbox integration.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_core.sandbox import Sandbox, SandboxResult
from .state import TestResult


def apply_patch_and_test(
    *,
    sandbox: Sandbox,
    repo_path: str,
    patch: str,
    test_command: str,
) -> TestResult:
    """Apply a unified diff in the sandbox and run the test command.

    Returns a TestResult with parsed pass/fail and extracted failure messages.
    """
    patch_file = f"{repo_path}/.agent_patch.diff"
    sandbox.write_file(patch_file, patch)

    apply = sandbox.run(f"git apply {patch_file}", cwd=repo_path)
    if not apply.success:
        return TestResult(
            success=False,
            output=apply.stderr or apply.stdout,
            failures=[f"patch apply failed:\n{apply.stderr}"],
        )

    result = sandbox.run(test_command, cwd=repo_path)
    failures = _parse_failures(result)
    return TestResult(
        success=result.success,
        output=result.stdout + result.stderr,
        failures=failures,
    )


def _parse_failures(result: SandboxResult) -> list[str]:
    """Extract failure/error lines from test output. Handles JUnit + pytest formats."""
    lines = (result.stdout + result.stderr).splitlines()
    failures: list[str] = []

    # JUnit / Gradle: "FAILED", "ERROR", "BUILD FAILED"
    junit_pattern = re.compile(r"(FAILED|ERROR|BUILD FAILED|CompilationError)", re.I)
    # pytest: "FAILED src/..." or "ERROR src/..."
    pytest_pattern = re.compile(r"^(FAILED|ERROR)\s+\S+", re.M)

    for line in lines:
        if junit_pattern.search(line):
            failures.append(line.strip())

    for match in pytest_pattern.finditer(result.stdout + result.stderr):
        failures.append(match.group(0).strip())

    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for f in failures:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return deduped[:20]  # cap at 20 so we don't flood the fix prompt
