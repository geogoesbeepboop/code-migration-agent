"""plan node — format the human-readable migration plan and estimate cost."""

from __future__ import annotations

import logging

from migration.profiles import load_profile
from migration.state import MigrationState

log = logging.getLogger(__name__)

# Rough token + cost estimates per file (Sonnet 4.6 pricing, 2025)
_INPUT_TOKENS_PER_FILE = 3_000    # rules + dep context + file content
_OUTPUT_TOKENS_PER_FILE = 800     # unified diff
_INPUT_USD_PER_1M = 3.0
_OUTPUT_USD_PER_1M = 15.0


def plan(state: MigrationState) -> MigrationState:
    """Build a human-readable migration plan and rough cost estimate.

    The migration_order is already set by ingest (topologically sorted + rules).
    This node formats it for the HITL approval gate and computes cost.
    """
    migration_order = state.get("migration_order", [])
    profile_name = state.get("profile_name", "unknown")
    n = len(migration_order)

    cost_per_file = (
        _INPUT_TOKENS_PER_FILE * _INPUT_USD_PER_1M
        + _OUTPUT_TOKENS_PER_FILE * _OUTPUT_USD_PER_1M
    ) / 1_000_000
    estimated_cost = n * cost_per_file

    lines = [
        f"=== Migration Plan ===",
        f"Profile:         {profile_name}",
        f"Files to migrate: {n}",
        f"Estimated cost:  ${estimated_cost:.4f} USD",
        f"",
        f"Migration order (dependency-first):",
    ]
    for i, task in enumerate(migration_order, 1):
        rules = ", ".join(task.get("rules", [])) or "(no rules triggered)"
        deps = len(task.get("deps", []))
        dep_str = f"  deps:{deps}" if deps else ""
        lines.append(f"  {i:3}. {task['path']}  [{rules}]{dep_str}")

    plan_summary = "\n".join(lines)
    log.info("Plan ready: %d files, est. $%.4f", n, estimated_cost)

    return {
        **state,
        "plan_summary": plan_summary,
        "estimated_cost_usd": estimated_cost,
        "current_file_index": 0,
        "fix_attempts": 0,
        "current_file_gave_up": False,
    }
