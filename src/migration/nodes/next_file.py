"""next_file node — commit the current file's patch and advance to the next file.

Sits between critic and (worker | pr). Handles:
- Committing accepted patches to git
- Tracking gave_up files
- Emitting FileEvalRecord for the scorecard
- Advancing current_file_index
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from migration.state import AppliedPatch, FileEvalRecord, MigrationState

log = logging.getLogger(__name__)


def next_file(state: MigrationState) -> MigrationState:
    """Commit the current file (if passed) and advance the index."""
    test_result = state.get("test_result", {})
    gave_up = state.get("current_file_gave_up", False)
    current_file = state.get("current_file", {})
    file_path = current_file.get("path", "")
    repo_path = Path(state.get("repo_path", ""))
    profile_name = state.get("profile_name", "")
    patch = state.get("patch", "")
    file_cost = state.get("current_file_cost_usd", 0.0)
    fix_attempts = state.get("fix_attempts", 0)

    applied_patches = list(state.get("applied_patches", []))
    gave_up_files = list(state.get("gave_up_files", []))
    eval_records = list(state.get("file_eval_records", []))

    success = test_result.get("success", False) and not gave_up
    commit_sha = ""

    if success and file_path:
        commit_sha = _commit_file(repo_path, file_path, profile_name)
        applied_patches.append(AppliedPatch(path=file_path, patch=patch, commit_sha=commit_sha))
        log.info("Committed migration of %s (%s)", file_path, commit_sha[:8])
    elif gave_up and file_path:
        _reset_file(repo_path, file_path)
        gave_up_files.append(file_path)
        log.info("Skipped %s (exhausted retries)", file_path)

    # Emit eval record for scorecard
    if file_path:
        eval_records.append(FileEvalRecord(
            path=file_path,
            success=success,
            gave_up=gave_up,
            fix_attempts=fix_attempts,
            cost_usd=file_cost,
            diff_size_lines=_count_diff_lines(patch),
            rules_applied=current_file.get("rules", []),
            critic_verdict=state.get("critic_verdict", ""),
        ))

    idx = state.get("current_file_index", 0)
    return {
        **state,
        "current_file_index": idx + 1,
        "fix_attempts": 0,
        "current_file_gave_up": False,
        "current_file_cost_usd": 0.0,
        "patch": "",
        "critic_verdict": "",
        "critic_notes": "",
        "applied_patches": applied_patches,
        "gave_up_files": gave_up_files,
        "file_eval_records": eval_records,
    }


def route_from_next_file(state: MigrationState) -> str:
    """Route to worker (more files) or pr (all done)."""
    idx = state.get("current_file_index", 0)
    migration_order = state.get("migration_order", [])
    return "worker" if idx < len(migration_order) else "pr"


def _commit_file(repo_path: Path, file_path: str, profile_name: str) -> str:
    subprocess.run(
        ["git", "add", "--", file_path],
        cwd=repo_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", f"chore: migrate {file_path} [{profile_name}]"],
        cwd=repo_path, check=True, capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=repo_path,
    )
    return result.stdout.strip()


def _reset_file(repo_path: Path, file_path: str) -> None:
    subprocess.run(
        ["git", "checkout", "HEAD", "--", file_path],
        capture_output=True, cwd=repo_path,
    )


def _count_diff_lines(patch: str) -> int:
    """Count added + removed lines in a unified diff."""
    return sum(1 for line in patch.splitlines() if line.startswith(("+", "-"))
               and not line.startswith(("+++", "---")))
