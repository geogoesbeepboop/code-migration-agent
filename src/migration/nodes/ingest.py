"""ingest node — clone repo into sandbox, parse all files, build dep graph."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from agent_core.sandbox import get_sandbox, is_docker_available
from migration.depgraph import build_dependency_graph, build_file_tasks
from migration.profiles import load_profile
from migration.rule_loader import load_rule_index
from migration.state import MigrationState

log = logging.getLogger(__name__)


def ingest(state: MigrationState) -> MigrationState:
    """Clone the repo, build dep graph + rule index, start sandbox.

    Sets: repo_path, dep_graph, migration_order, sandbox_container_id / e2b_sandbox_id
    """
    import git  # gitpython

    repo_url = state.get("repo_url", "")
    profile_name = state.get("profile_name", "")
    if not repo_url:
        raise ValueError("repo_url must be set in state before ingest")
    if not profile_name:
        raise ValueError("profile_name must be set in state before ingest")

    # ── clone ─────────────────────────────────────────────────────────────────
    tmp = tempfile.mkdtemp(prefix="migration_")
    log.info("Cloning %s → %s", repo_url, tmp)
    git.Repo.clone_from(repo_url, tmp)

    # Configure git identity so commits work without a global git config
    subprocess.run(["git", "config", "user.email", "migration-agent@local"],
                   cwd=tmp, capture_output=True)
    subprocess.run(["git", "config", "user.name", "migration-agent"],
                   cwd=tmp, capture_output=True)

    repo_path = Path(tmp)
    profile = load_profile(profile_name)

    # ── dep graph ─────────────────────────────────────────────────────────────
    log.info("Building dependency graph (profile=%s  glob=%s)", profile_name, profile.source_glob)
    dep_graph = build_dependency_graph(repo_path, profile.source_glob)
    rule_index = load_rule_index(profile.rules_path)
    migration_order = build_file_tasks(dep_graph, rule_index, repo_path, profile.source_glob)
    log.info("Migration order: %d files", len(migration_order))

    # ── sandbox ───────────────────────────────────────────────────────────────
    backend = os.environ.get("SANDBOX_BACKEND", "local").lower()
    sandbox_container_id = ""
    e2b_sandbox_id = ""

    if backend == "docker":
        if not is_docker_available():
            log.warning("Docker not available — falling back to local sandbox")
            os.environ["SANDBOX_BACKEND"] = "local"
        else:
            sandbox = get_sandbox(repo_path=str(repo_path), image=profile.sandbox_image)
            sandbox_container_id = sandbox.backend_id
            log.info("DockerSandbox ready: %s  image=%s", sandbox_container_id[:12], profile.sandbox_image)

    elif backend == "e2b":
        sandbox = get_sandbox(repo_path=str(repo_path))
        e2b_sandbox_id = sandbox.backend_id
        log.info("E2BSandbox ready: %s", e2b_sandbox_id)

    run_id = state.get("run_id") or str(uuid.uuid4())

    return {
        **state,
        "repo_path": str(repo_path),
        "dep_graph": dep_graph.to_dict(),
        "migration_order": migration_order,
        "applied_patches": [],
        "gave_up_files": [],
        "file_eval_records": [],
        "errors": state.get("errors", []),
        "total_cost_usd": 0.0,
        "current_file_cost_usd": 0.0,
        "human_interventions": 0,
        "sandbox_container_id": sandbox_container_id,
        "e2b_sandbox_id": e2b_sandbox_id,
        "run_id": run_id,
    }
