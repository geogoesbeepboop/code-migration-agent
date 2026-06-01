"""HITL gate configuration.

Three gates, each toggleable via HITL_LEVEL env var:
  full       — plan approval · give-up escalation · PR review
  plan_only  — plan approval only
  none       — fully autonomous (CI / benchmarking)

Gate 2 mode (give-up escalation) is controlled separately via HITL_GATE2:
  immediate — NodeInterrupt fires inside critic per file (original behaviour)
  deferred  — all give-up files are grouped and shown once before the PR gate
               (default; saves interruptions and LLM calls)

Gate placement:
  Gate 1 (plan approval):    interrupt_before=["plan_review"]       — once, before first file
  Gate 2 (give-up):          NodeInterrupt in critic (immediate)
                             OR interrupt_before=["resolve_give_ups"] (deferred)
  Gate 3 (PR review):        interrupt_before=["pr"]                — once, before PR opens
"""

import os
from enum import Enum


class HITLLevel(str, Enum):
    FULL = "full"
    PLAN_ONLY = "plan_only"
    NONE = "none"


class HITLGate2Mode(str, Enum):
    IMMEDIATE = "immediate"
    DEFERRED = "deferred"


def get_hitl_level() -> HITLLevel:
    raw = os.environ.get("HITL_LEVEL", "full").lower()
    try:
        return HITLLevel(raw)
    except ValueError:
        return HITLLevel.FULL


def get_gate2_mode() -> HITLGate2Mode:
    raw = os.environ.get("HITL_GATE2", "deferred").lower()
    try:
        return HITLGate2Mode(raw)
    except ValueError:
        return HITLGate2Mode.DEFERRED


def hitl_gates() -> list[str]:
    """Return node names for interrupt_before= in graph.compile()."""
    level = get_hitl_level()
    gate2 = get_gate2_mode()

    if level == HITLLevel.NONE:
        return []
    if level == HITLLevel.PLAN_ONLY:
        return ["plan_review"]

    # FULL level
    gates = ["plan_review", "pr"]
    if gate2 == HITLGate2Mode.DEFERRED:
        gates.append("resolve_give_ups")
    return gates
