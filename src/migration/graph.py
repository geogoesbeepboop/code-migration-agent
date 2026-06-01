"""LangGraph workflow definition — the spine of the migration platform.

Topology:

    ingest → plan → [HITL1] plan_review → worker → verify
                                              ↑           ↓ pass → critic
                                              │           ↓ fix  → fix → verify
                                              │           ↓ give_up → mark_give_up → critic
                                              │                            ↓ [HITL2-immediate: NodeInterrupt]
                                         next_file ←──────────────────────┘
                                              │
                                              ├── more files → worker
                                              └── all done  → [HITL2-deferred] resolve_give_ups
                                                                    ↓ [HITL3] pr → END

Verify Phase 0: python -c "from migration.graph import build_graph; print('ok')"
"""

from __future__ import annotations

import os

from langgraph.graph import END, StateGraph

from .hitl import hitl_gates
from .nodes import (
    critic,
    fix,
    ingest,
    mark_give_up,
    next_file,
    plan,
    plan_review,
    pr,
    resolve_give_ups,
    route_from_next_file,
    verify,
    worker,
)
from .nodes.verify import route_after_verify
from .state import MigrationState


def build_graph():
    """Construct and compile the migration StateGraph."""
    g = StateGraph(MigrationState)

    for name, fn in [
        ("ingest",            ingest),
        ("plan",              plan),
        ("plan_review",       plan_review),
        ("worker",            worker),
        ("verify",            verify),
        ("fix",               fix),
        ("mark_give_up",      mark_give_up),
        ("critic",            critic),
        ("next_file",         next_file),
        ("resolve_give_ups",  resolve_give_ups),
        ("pr",                pr),
    ]:
        g.add_node(name, fn)

    # ── linear spine ──────────────────────────────────────────────────────────
    g.set_entry_point("ingest")
    g.add_edge("ingest",        "plan")
    g.add_edge("plan",          "plan_review")   # HITL gate 1 fires before plan_review
    g.add_edge("plan_review",   "worker")
    g.add_edge("worker",        "verify")

    # ── verify routing ────────────────────────────────────────────────────────
    g.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "pass":     "critic",
            "fix":      "fix",
            "give_up":  "mark_give_up",   # sets current_file_gave_up=True, then critic
        },
    )
    g.add_edge("fix",          "verify")
    g.add_edge("mark_give_up", "critic")   # critic: immediate mode raises HITL gate 2;
                                            #         deferred mode logs and continues

    # ── critic → next_file ────────────────────────────────────────────────────
    g.add_edge("critic", "next_file")

    # ── next_file: loop or done ───────────────────────────────────────────────
    g.add_conditional_edges(
        "next_file",
        route_from_next_file,
        {
            "worker":         "worker",           # more files to migrate
            "resolve_give_ups": "resolve_give_ups",  # all files done → check give-ups
        },
    )

    # resolve_give_ups → pr (HITL gate 2 deferred fires before resolve_give_ups;
    # gate 3 fires before pr)
    g.add_edge("resolve_give_ups", "pr")
    g.add_edge("pr", END)

    # ── compile with checkpointing + HITL gates ───────────────────────────────
    checkpointer = _build_checkpointer()
    gates = hitl_gates()

    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=gates,
    )


def _build_checkpointer():
    db_path = os.environ.get("AGENT_CHECKPOINT_DB", ".agent-core/checkpoints.db")
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        return SqliteSaver(conn)
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()
