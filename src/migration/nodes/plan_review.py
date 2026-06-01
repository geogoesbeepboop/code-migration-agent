"""plan_review node — pass-through that serves as the HITL gate 1 interrupt point.

interrupt_before=["plan_review"] fires once before migration starts.
The human sees plan_summary in the state, then resumes to approve.
This node is NOT in the per-file loop, so it fires exactly once.
"""

from __future__ import annotations

import logging

from migration.state import MigrationState

log = logging.getLogger(__name__)


def plan_review(state: MigrationState) -> MigrationState:
    """Human has approved the plan. Log and pass through to worker."""
    n = len(state.get("migration_order", []))
    log.info("Plan approved — starting migration of %d files", n)
    return state
