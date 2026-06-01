"""critic node — LLM judge: is the diff idiomatic, minimal, behavior-preserving?

HITL gate 2 fires here when the file gave up (exhausted retries).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from agent_core.models import Tier, complete_with_cost, get_run_budget
from migration.state import MigrationState

log = logging.getLogger(__name__)

_SYSTEM = """\
You are a senior code reviewer specialising in Java-to-Kotlin migrations.
Evaluate the migration diff on exactly three axes:

1. Idiomatic — does it use Kotlin/target idioms correctly?
2. Minimal — does it change ONLY what the migration rules require?
3. Behavior-preserving — does it maintain the original semantics?

Respond with ONLY a JSON object on a single line:
{"verdict": "approve"|"revise"|"escalate", "notes": "<one concise paragraph>"}

Use "approve" if all three axes pass.
Use "revise" if there are fixable issues you can describe.
Use "escalate" if the transformation is too risky or complex to automate.
"""

_PROMPT = """\
## Profile: {profile_name}
## File: {path}
## Test result: {test_status}
## Fix attempts: {fix_attempts}

## Diff:
```diff
{patch}
```

Evaluate the diff. Output only the JSON verdict.
"""


def critic(state: MigrationState) -> MigrationState:
    """LLM-judge the diff. Sets critic_verdict and critic_notes.

    HITL gate 2: fires when current_file_gave_up is True (NodeInterrupt).
    """
    from langgraph.errors import NodeInterrupt

    gave_up = state.get("current_file_gave_up", False)
    current_file = state.get("current_file", {})
    file_path = current_file.get("path", "unknown")

    # HITL gate 2: escalate files that exhausted retries
    if gave_up:
        msg = (
            f"HITL GATE 2 — Escalation required.\n\n"
            f"File: {file_path}\n"
            f"Fix attempts: {state.get('fix_attempts', 0)}\n"
            f"Last failure:\n{state.get('test_result', {}).get('output', '')[:500]}\n\n"
            "Options: resume to skip this file / edit manually then resume / stop the run."
        )
        raise NodeInterrupt(msg)

    # Normal path: LLM review of the diff
    test_result = state.get("test_result", {})
    test_status = "PASSED" if test_result.get("success") else "FAILED"
    patch = state.get("patch", "")
    profile_name = state.get("profile_name", "")
    fix_attempts = state.get("fix_attempts", 0)

    if not patch.strip():
        # No changes — auto-approve
        return {
            **state,
            "critic_verdict": "approve",
            "critic_notes": "No changes produced — file may already be idiomatic.",
        }

    prompt = _PROMPT.format(
        profile_name=profile_name,
        path=file_path,
        test_status=test_status,
        fix_attempts=fix_attempts,
        patch=patch[:6000],  # cap for context
    )

    run_id = state.get("run_id", "default")
    budget = get_run_budget(run_id)

    response, cost = complete_with_cost(prompt, system=_SYSTEM, tier=Tier.MID,
                                         max_tokens=512, budget=budget)
    verdict, notes = _parse_critic_response(response)

    log.info("Critic verdict for %s: %s", file_path, verdict)
    return {
        **state,
        "critic_verdict": verdict,
        "critic_notes": notes,
        "current_file_cost_usd": state.get("current_file_cost_usd", 0.0) + cost,
        "total_cost_usd": state.get("total_cost_usd", 0.0) + cost,
    }


def _parse_critic_response(text: str) -> tuple[str, str]:
    """Parse {"verdict": ..., "notes": ...} from LLM output."""
    text = text.strip()

    # Try to find a JSON object anywhere in the response
    json_match = re.search(r'\{[^}]+\}', text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            verdict = data.get("verdict", "approve")
            notes = data.get("notes", "")
            if verdict not in ("approve", "revise", "escalate"):
                verdict = "approve"
            return verdict, notes
        except json.JSONDecodeError:
            pass

    # Fallback: look for verdict keyword
    text_lower = text.lower()
    if "escalate" in text_lower:
        return "escalate", text
    if "revise" in text_lower:
        return "revise", text
    return "approve", text
