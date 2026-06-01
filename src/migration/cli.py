"""CLI entry point for the migration agent.

Usage:
    python -m migration.cli --repo <url> --profile java_to_kotlin [--hitl full|plan_only|none]

Resume an interrupted run:
    python -m migration.cli --resume <thread-id>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="code-migration-agent")
    parser.add_argument("--repo", help="Git repo URL to migrate")
    parser.add_argument("--profile", default="java_to_kotlin", help="Migration profile")
    parser.add_argument(
        "--hitl",
        choices=["full", "plan_only", "none"],
        default="full",
        help="HITL gate level (default: full)",
    )
    parser.add_argument("--resume", metavar="THREAD_ID", help="Resume an interrupted run")
    parser.add_argument("--thread-id", help="Thread ID to use (for checkpointing)")
    args = parser.parse_args()

    # Apply HITL level
    os.environ["HITL_LEVEL"] = args.hitl

    from migration.graph import build_graph

    graph = build_graph()
    thread_id = args.thread_id or args.resume or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    if args.resume:
        log.info("Resuming thread %s", thread_id)
        _run_graph(graph, None, config)
    elif args.repo:
        initial_state = {
            "repo_url": args.repo,
            "profile_name": args.profile,
            "errors": [],
        }
        log.info("Starting migration: %s → profile=%s  thread=%s", args.repo, args.profile, thread_id)
        _run_graph(graph, initial_state, config)
    else:
        parser.print_help()
        sys.exit(1)


def _run_graph(graph, state, config: dict) -> None:
    for event in graph.stream(state, config, stream_mode="values"):
        _print_event(event)

    # Check if interrupted
    snapshot = graph.get_state(config)
    if snapshot.next:
        thread_id = config["configurable"]["thread_id"]
        interrupts = getattr(snapshot, "tasks", [])
        interrupt_msg = ""
        for task in interrupts:
            if hasattr(task, "interrupts") and task.interrupts:
                for intr in task.interrupts:
                    interrupt_msg = str(intr.value)
                    break

        print("\n" + "=" * 60)
        print("⏸  INTERRUPTED")
        if interrupt_msg:
            print(interrupt_msg)
        print(f"\nTo resume: python -m migration.cli --resume {thread_id}")
        print("=" * 60)
    else:
        final = snapshot.values
        print("\n" + "=" * 60)
        print("✅ MIGRATION COMPLETE")
        print(f"   PR:      {final.get('pr_url', '(none)')}")
        print(f"   Files:   {len(final.get('applied_patches', []))} migrated, "
              f"{len(final.get('gave_up_files', []))} skipped")
        print(f"   Cost:    ${final.get('total_cost_usd', 0.0):.4f} USD")
        print("=" * 60)


def _print_event(event: dict) -> None:
    # Print a brief status line for each state update
    idx = event.get("current_file_index", 0)
    order = event.get("migration_order", [])
    if order and 0 <= idx < len(order):
        path = order[idx].get("path", "?")
        log.info("[%d/%d] current: %s", idx + 1, len(order), path)

    plan = event.get("plan_summary")
    if plan and not event.get("current_file_index"):
        print("\n" + plan + "\n")


if __name__ == "__main__":
    main()
