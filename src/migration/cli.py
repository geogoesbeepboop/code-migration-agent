"""CLI entry point for the migration agent.

Usage:
    python -m migration.cli --repo <url> --profile java_to_kotlin [--hitl full|plan_only|none]

The CLI stays alive through all HITL gates — no need to run --resume manually.
If you need to resume a previous session:
    python -m migration.cli --resume <thread-id>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger(__name__)

# Gate identifiers embedded in NodeInterrupt messages so the CLI knows which
# state key to update on a "n" (redo) response.
_GATE1_MARKER = "HITL GATE 1"
_GATE2_MARKER = "HITL GATE 2"
_GATE3_MARKER = "HITL GATE 3"


def main() -> None:
    parser = argparse.ArgumentParser(description="code-migration-agent")
    sub = parser.add_subparsers(dest="command")

    # Default (migrate) command
    migrate_p = sub.add_parser("migrate", help="Run a migration")
    migrate_p.add_argument("--repo", required=True, help="Git repo URL to migrate")
    migrate_p.add_argument("--profile", default="java_to_kotlin", help="Migration profile")
    migrate_p.add_argument(
        "--hitl",
        choices=["full", "plan_only", "none"],
        default="full",
        help="HITL gate level (default: full)",
    )
    migrate_p.add_argument(
        "--hitl-gate2",
        choices=["immediate", "deferred"],
        default="deferred",
        help="Give-up gate 2 mode: immediate (per-file) or deferred (grouped before PR)",
    )
    migrate_p.add_argument("--thread-id", help="Thread ID for checkpointing")

    # Resume command
    resume_p = sub.add_parser("resume", help="Resume an interrupted run")
    resume_p.add_argument("thread_id", help="Thread ID to resume")
    resume_p.add_argument(
        "--hitl",
        choices=["full", "plan_only", "none"],
        default="full",
    )
    resume_p.add_argument(
        "--hitl-gate2",
        choices=["immediate", "deferred"],
        default="deferred",
    )

    # Scaffold profile command
    scaffold_p = sub.add_parser("scaffold-profile", help="Generate a new migration profile using LLM")
    scaffold_p.add_argument("--name", required=True, help="Profile directory name (e.g. spring_boot_2_to_3)")
    scaffold_p.add_argument("--from", dest="from_version", required=True, help="Source version/framework")
    scaffold_p.add_argument("--to", dest="to_version", required=True, help="Target version/framework")
    scaffold_p.add_argument("--sources", nargs="*", default=[], help="URLs or file paths with migration notes")
    scaffold_p.add_argument("--test-command", default="", help="Test command for the profile (e.g. ./gradlew test)")
    scaffold_p.add_argument("--source-glob", default="**/*.java", help="Source file glob")
    scaffold_p.add_argument("--target-ext", default=".java", help="Target file extension")
    scaffold_p.add_argument("--sandbox-image", default="eclipse-temurin:21", help="Docker sandbox image")

    # Legacy flat args for backward compatibility (no subcommand)
    parser.add_argument("--repo", help=argparse.SUPPRESS)
    parser.add_argument("--profile", default="java_to_kotlin", help=argparse.SUPPRESS)
    parser.add_argument("--hitl", choices=["full", "plan_only", "none"], default="full", help=argparse.SUPPRESS)
    parser.add_argument("--hitl-gate2", choices=["immediate", "deferred"], default="deferred", help=argparse.SUPPRESS)
    parser.add_argument("--resume", metavar="THREAD_ID", help=argparse.SUPPRESS)
    parser.add_argument("--thread-id", help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Handle subcommands
    if args.command == "scaffold-profile":
        _run_scaffold(args)
        return

    # Determine mode: subcommand or legacy flat args
    if args.command == "migrate":
        repo = args.repo
        profile = args.profile
        hitl = args.hitl
        hitl_gate2 = args.hitl_gate2
        thread_id = args.thread_id or str(uuid.uuid4())
        resume_thread = None
    elif args.command == "resume":
        repo = None
        profile = None
        hitl = args.hitl
        hitl_gate2 = args.hitl_gate2
        thread_id = args.thread_id
        resume_thread = args.thread_id
    else:
        # Legacy flat args
        repo = args.repo
        profile = args.profile
        hitl = args.hitl
        hitl_gate2 = args.hitl_gate2
        thread_id = args.thread_id or args.resume or str(uuid.uuid4())
        resume_thread = args.resume

    os.environ["HITL_LEVEL"] = hitl
    os.environ["HITL_GATE2"] = hitl_gate2

    from migration.graph import build_graph

    graph = build_graph()
    config = {"configurable": {"thread_id": thread_id}}

    if resume_thread:
        log.info("Resuming thread %s", thread_id)
        _run_interactive(graph, None, config)
    elif repo:
        initial_state = {
            "repo_url": repo,
            "profile_name": profile,
            "errors": [],
        }
        log.info("Starting migration: %s → profile=%s  thread=%s", repo, profile, thread_id)
        _run_interactive(graph, initial_state, config)
    else:
        parser.print_help()
        sys.exit(1)


def _run_interactive(graph, initial_state, config: dict) -> None:
    """Run the graph with an inline HITL loop.

    Instead of exiting after each interrupt and asking the user to --resume,
    this loop stays alive and prompts y/n at each gate, then resumes or injects
    feedback and re-runs as appropriate.
    """
    state = initial_state

    while True:
        # Stream until the graph pauses or finishes
        for event in graph.stream(state, config, stream_mode="values"):
            _print_event(event)

        snapshot = graph.get_state(config)

        if not snapshot.next:
            # Graph completed
            _print_final(snapshot.values)
            return

        # Graph interrupted — collect interrupt message
        interrupt_msg, gate = _extract_interrupt(snapshot)

        print("\n" + "=" * 60)
        print("⏸  INTERRUPTED" + (f" — {gate}" if gate else ""))
        if interrupt_msg:
            print(interrupt_msg)
        print("=" * 60)

        approved, feedback = _prompt_hitl(gate)

        if approved:
            # Resume without injecting state changes
            state = None
        else:
            # "n" path: inject feedback and route back to the appropriate node
            state = _build_feedback_state(snapshot.values, gate, feedback)
            _reroute_on_feedback(graph, config, gate)

        # Next iteration will resume (state=None resumes from checkpoint)


def _extract_interrupt(snapshot) -> tuple[str, str]:
    """Return (interrupt_message, gate_label) from a paused snapshot."""
    interrupt_msg = ""
    gate = ""
    for task in getattr(snapshot, "tasks", []):
        if hasattr(task, "interrupts") and task.interrupts:
            interrupt_msg = str(task.interrupts[0].value)
            break

    # Detect which gate we're at from the message or next-node name
    next_nodes = list(snapshot.next) if snapshot.next else []
    if _GATE2_MARKER in interrupt_msg:
        gate = "gate2"
    elif any("plan_review" in n for n in next_nodes):
        gate = "gate1"
    elif any("pr" in n for n in next_nodes):
        gate = "gate3"
    elif any("resolve_give_ups" in n for n in next_nodes):
        gate = "gate2_deferred"
    return interrupt_msg, gate


def _prompt_hitl(gate: str) -> tuple[bool, str]:
    """Show a y/n prompt and return (approved, feedback_text)."""
    gate_labels = {
        "gate1": "Plan",
        "gate2": "Give-up escalation",
        "gate2_deferred": "Give-up review (all failures)",
        "gate3": "PR review",
    }
    label = gate_labels.get(gate, "Review")

    try:
        answer = input(f"\n[{label}] Approve? [y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)

    if answer == "y":
        return True, ""

    # "n" — ask for feedback
    try:
        feedback = input("Enter feedback / instructions for the agent: ").strip()
    except (EOFError, KeyboardInterrupt):
        feedback = ""
    return False, feedback


def _build_feedback_state(current_values: dict, gate: str, feedback: str) -> dict:
    """Return a state update dict that injects user feedback for the given gate."""
    if gate == "gate1":
        return {**current_values, "user_plan_feedback": feedback}
    elif gate in ("gate2", "gate2_deferred"):
        return {**current_values, "user_fix_instructions": feedback}
    elif gate == "gate3":
        return {**current_values, "user_pr_feedback": feedback}
    return current_values


def _reroute_on_feedback(graph, config: dict, gate: str) -> None:
    """Update the graph checkpoint to route back to the correct node after feedback."""
    if gate == "gate1":
        # Re-run plan node so it incorporates user_plan_feedback
        graph.update_state(config, {}, as_node="plan_review")
    elif gate in ("gate2", "gate2_deferred"):
        # Re-run from worker so the file is migrated again with user_fix_instructions
        graph.update_state(config, {}, as_node="critic")
    # gate3: just resume — pr node will pick up user_pr_feedback from state


def _print_event(event: dict) -> None:
    idx = event.get("current_file_index", 0)
    order = event.get("migration_order", [])
    if order and 0 <= idx < len(order):
        path = order[idx].get("path", "?")
        log.info("[%d/%d] current: %s", idx + 1, len(order), path)

    plan = event.get("plan_summary")
    if plan and not event.get("current_file_index"):
        print("\n" + plan + "\n")


def _print_final(final: dict) -> None:
    print("\n" + "=" * 60)
    print("✅ MIGRATION COMPLETE")
    print(f"   PR:      {final.get('pr_url', '(none)')}")
    print(f"   Files:   {len(final.get('applied_patches', []))} migrated, "
          f"{len(final.get('gave_up_files', []))} skipped")
    print(f"   Cost:    ${final.get('total_cost_usd', 0.0):.4f} USD")
    if final.get("langfuse_trace_url"):
        print(f"   Trace:   {final['langfuse_trace_url']}")
    print("=" * 60)


def _run_scaffold(args) -> None:
    from migration.scaffold import scaffold_profile
    scaffold_profile(
        name=args.name,
        from_version=args.from_version,
        to_version=args.to_version,
        sources=args.sources,
        test_command=args.test_command,
        source_glob=args.source_glob,
        target_ext=args.target_ext,
        sandbox_image=args.sandbox_image,
    )


if __name__ == "__main__":
    main()
