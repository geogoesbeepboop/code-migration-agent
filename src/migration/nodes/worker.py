"""worker node — load file rules + dep context, call LLM, emit unified diff."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from agent_core.models import Tier, complete_with_cost, get_run_budget
from migration.profiles import load_profile
from migration.rule_loader import load_rule_index
from migration.state import MigrationState

log = logging.getLogger(__name__)

_SYSTEM = """\
You are an expert code migration assistant specialising in automated source-code
transformations. Your task is to migrate a single source file according to a set
of migration rules.

Output ONLY a valid unified diff (---/+++ headers, @@ hunks). Do NOT include any
explanation, markdown fence, or text outside the diff. If no changes are needed,
output an empty string.

Unified diff format:
  --- a/<original-path>
  +++ b/<migrated-path>
  @@ -<start>,<count> +<start>,<count> @@
   <context line>
  -<removed line>
  +<added line>
"""

_PROMPT = """\
## File to migrate: {path}

## Source content:
```java
{content}
```

## Rules to apply:
{rules_text}

## Already-migrated dependency context:
{dep_context}

Produce the unified diff to migrate this file. Output ONLY the diff.
"""


def worker(state: MigrationState) -> MigrationState:
    """Migrate the current file using LLM Tier.MID (Sonnet).

    HITL gate 1 fires before the first call via interrupt_before=["plan_review"].
    """
    migration_order = state.get("migration_order", [])
    idx = state.get("current_file_index", 0)

    if not migration_order:
        raise ValueError("migration_order is empty — did ingest and plan run?")
    if idx >= len(migration_order):
        raise ValueError(f"current_file_index {idx} out of range ({len(migration_order)} files)")

    task = migration_order[idx]
    profile_name = state.get("profile_name", "")
    repo_path = Path(state.get("repo_path", ""))

    log.info("[%d/%d] Migrating %s", idx + 1, len(migration_order), task["path"])

    # Load file content (also stored as original for critic evaluation)
    abs_path = repo_path / task["path"]
    content = abs_path.read_text(errors="replace")
    original_src = content

    # Load rule text for prompt
    profile = load_profile(profile_name)
    rule_index = load_rule_index(profile.rules_path, profile.keywords_path)
    rules_text = rule_index.rule_text(task.get("rules", []))
    if not rules_text.strip():
        rules_text = "(No specific rules triggered — apply general idiomatic Kotlin improvements.)"

    # Build dep context from already-committed neighbours
    dep_context = _build_dep_context(task.get("deps", []), repo_path)

    # Prepend any user fix instructions from a previous gate 2 "n" response
    user_instructions = state.get("user_fix_instructions", "").strip()
    user_instructions_section = (
        f"\n## User instructions (override):\n{user_instructions}\n" if user_instructions else ""
    )

    prompt = _PROMPT.format(
        path=task["path"],
        content=content[:12_000],  # cap to avoid overwhelming context
        rules_text=rules_text,
        dep_context=dep_context,
    ) + user_instructions_section

    run_id = state.get("run_id", "default")
    budget = get_run_budget(run_id)

    response, cost = complete_with_cost(prompt, system=_SYSTEM, tier=Tier.MID,
                                         max_tokens=4096, budget=budget)
    patch = _extract_diff(response)

    return {
        **state,
        "current_file": task,
        "patch": patch,
        "fix_attempts": 0,
        "current_file_gave_up": False,
        "current_file_original_src": original_src,
        "current_file_cost_usd": cost,   # reset for this file
        "total_cost_usd": state.get("total_cost_usd", 0.0) + cost,
        "user_fix_instructions": "",     # consumed; clear for next file
    }


def _build_dep_context(deps: list[str], repo_path: Path) -> str:
    if not deps:
        return "(none)"
    lines: list[str] = []
    for dep_path in deps[:5]:  # cap at 5 deps
        abs_dep = repo_path / dep_path
        try:
            content = abs_dep.read_text(errors="replace")
            preview = "\n".join(content.splitlines()[:60])
            lines.append(f"### {dep_path}\n```\n{preview}\n```")
        except OSError:
            lines.append(f"### {dep_path} (not readable)")
    return "\n\n".join(lines)


def _extract_diff(text: str) -> str:
    """Extract a unified diff from LLM output, stripping any markdown fences."""
    fenced = re.search(r"```(?:diff)?\n(.*?)```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    text = text.strip()
    if text.startswith("---") or text.startswith("diff --git"):
        return text
    diff_start = re.search(r"^(---|\+\+\+|diff --git)", text, re.MULTILINE)
    if diff_start:
        return text[diff_start.start():].strip()
    return text
