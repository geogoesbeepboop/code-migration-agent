"""fix node — read test failure, revise patch using LLM Tier.HARD."""

from __future__ import annotations

import logging
from pathlib import Path

from agent_core.models import Tier, complete_with_cost, get_run_budget
from migration.nodes.worker import _extract_diff
from migration.state import MigrationState

log = logging.getLogger(__name__)

_SYSTEM = """\
You are an expert code migration debugger. A patch was applied to migrate a Java
file but the tests failed. Your job is to produce a corrected unified diff.

Output ONLY a valid unified diff. Do NOT include any explanation or markdown.
If the patch was fundamentally correct but had a syntax error, fix the syntax.
If the approach was wrong, try a different approach based on the failure message.
"""

_PROMPT = """\
## File: {path}

## Original source (before any patch):
```java
{original_content}
```

## Attempted patch:
```diff
{patch}
```

## Test failures:
```
{failures}
```

Produce a corrected unified diff that fixes the failures.
"""


def fix(state: MigrationState) -> MigrationState:
    """Revise the patch based on test failures using Tier.HARD (Opus)."""
    current_file = state.get("current_file", {})
    file_path = current_file.get("path", "")
    repo_path = Path(state.get("repo_path", ""))
    patch = state.get("patch", "")
    test_result = state.get("test_result", {})
    fix_attempts = state.get("fix_attempts", 0)

    failures = test_result.get("failures", [])
    failure_text = "\n".join(failures) if failures else test_result.get("output", "")[:2000]

    # Read original content (HEAD version, before any patch)
    abs_path = repo_path / file_path
    try:
        import subprocess
        orig_result = subprocess.run(
            ["git", "show", f"HEAD:{file_path}"],
            capture_output=True,
            text=True,
            cwd=repo_path,
        )
        original_content = orig_result.stdout if orig_result.returncode == 0 else abs_path.read_text(errors="replace")
    except Exception:
        original_content = abs_path.read_text(errors="replace")

    log.info(
        "[fix attempt %d/%s] %s — failures: %d",
        fix_attempts + 1,
        state.get("fix_attempts", "?"),
        file_path,
        len(failures),
    )

    prompt = _PROMPT.format(
        path=file_path,
        original_content=original_content[:8000],  # cap for context length
        patch=patch,
        failures=failure_text[:3000],
    )

    run_id = state.get("run_id", "default")
    budget = get_run_budget(run_id)

    response, cost = complete_with_cost(prompt, system=_SYSTEM, tier=Tier.HARD,
                                         max_tokens=4096, budget=budget)
    new_patch = _extract_diff(response)

    return {
        **state,
        "patch": new_patch,
        "fix_attempts": fix_attempts + 1,
        "current_file_cost_usd": state.get("current_file_cost_usd", 0.0) + cost,
        "total_cost_usd": state.get("total_cost_usd", 0.0) + cost,
    }
