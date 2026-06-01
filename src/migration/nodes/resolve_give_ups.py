"""resolve_give_ups node — grouped HITL gate 2 (deferred mode).

In deferred mode (HITL_GATE2=deferred, the default), individual file failures
do not interrupt the run. Instead, all gave-up files are accumulated in
gave_up_files and presented together here, just before the PR gate.

This node raises a single NodeInterrupt listing every failed file, so the
user can review them all at once and provide instructions before the PR is opened.
"""

from __future__ import annotations

import logging

from migration.state import MigrationState

log = logging.getLogger(__name__)


def resolve_give_ups(state: MigrationState) -> MigrationState:
    """Raise a grouped HITL gate 2 interrupt when deferred failures exist.

    If there are no give-up files, this node is a no-op and the run continues
    straight to the PR gate.
    """
    from langgraph.errors import NodeInterrupt

    gave_up_files = state.get("gave_up_files", [])
    if not gave_up_files:
        log.info("No give-up files — skipping resolve_give_ups gate")
        return state

    eval_records = {r["path"]: r for r in state.get("file_eval_records", [])}

    lines = [
        "HITL GATE 2 (deferred) — The following files exhausted all fix attempts:\n"
    ]
    for f in gave_up_files:
        rec = eval_records.get(f, {})
        attempts = rec.get("fix_attempts", "?")
        last_output = rec.get("last_test_output", "")
        lines.append(f"  • {f}  (fix attempts: {attempts})")
        if last_output:
            lines.append(f"    Last failure: {last_output[:300]}")

    lines += [
        "",
        "Options:",
        "  [y] Accept all skipped files as-is and continue to PR",
        "  [n] Provide instructions — enter guidance and the agent will retry each file",
    ]

    msg = "\n".join(lines)
    log.info("Raising grouped give-up gate for %d files", len(gave_up_files))
    raise NodeInterrupt(msg)
