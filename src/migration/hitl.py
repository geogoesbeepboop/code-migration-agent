"""HITL gate configuration.

Three gates, each toggleable via HITL_LEVEL env var:
  full       — plan approval · give-up escalation · PR review
  plan_only  — plan approval only
  none       — fully autonomous (CI / benchmarking)

Gate placement:
  Gate 1 (plan approval):    interrupt_before=["plan_review"]  — fires once, before first file
  Gate 2 (give-up escalation): NodeInterrupt inside critic     — fires per file on exhausted retries
  Gate 3 (PR review):        interrupt_before=["pr"]           — fires once, before PR opens
"""

import os
from enum import Enum


class HITLLevel(str, Enum):
    FULL = "full"
    PLAN_ONLY = "plan_only"
    NONE = "none"


def get_hitl_level() -> HITLLevel:
    raw = os.environ.get("HITL_LEVEL", "full").lower()
    try:
        return HITLLevel(raw)
    except ValueError:
        return HITLLevel.FULL


def hitl_gates() -> list[str]:
    """Return node names for interrupt_before= in graph.compile()."""
    level = get_hitl_level()
    if level == HITLLevel.FULL:
        return ["plan_review", "pr"]   # gate 1 + gate 3
    if level == HITLLevel.PLAN_ONLY:
        return ["plan_review"]         # gate 1 only
    return []                           # fully autonomous
