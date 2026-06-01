"""scaffold_profile — LLM-assisted migration profile generator.

Usage via CLI:
    python -m migration.cli scaffold-profile \
      --name spring_boot_2_to_3 \
      --from "Spring Boot 2.x" \
      --to "Spring Boot 3.x" \
      --sources https://spring.io/blog/migration-guide path/to/notes.txt \
      --test-command "./gradlew test --rerun-tasks --no-daemon" \
      --sandbox-image "gradle:8-jdk21"

The LLM produces:
  - rules.md      (numbered migration rules in the standard format)
  - keywords.toml (source + import keyword patterns for each rule)
  - tests.toml    (build config)
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

log = logging.getLogger(__name__)

_SCAFFOLD_SYSTEM = """\
You are a migration engineer creating a structured rule catalog for an automated
code migration tool. Given source and target versions plus any supplied migration notes,
produce two artefacts in exactly the formats described below.

--- ARTEFACT 1: rules.md ---
A markdown file where each rule follows this exact pattern (separated by ---):

## R<N> — <Short Title>

**Pattern:** <Description of the code pattern that should be detected in the source>

**Transform:** <Description of how to transform that pattern in the target>

**Complexity:** low | medium | high

---

Rules should be numbered R01, R02, … with no gaps.
Include 8–20 rules covering the most important migration changes.
Be specific and actionable — each rule should describe ONE transformation.

--- ARTEFACT 2: keywords.toml ---
A TOML file with two tables for matching rules to files:

[source_keywords]
# Strings searched in raw source text (lowercase, case-insensitive match)
R01 = ["keyword1", "keyword2"]

[import_keywords]
# Import path fragments that indicate the rule applies
R01 = ["com.example.OldClass"]

Only include rules that have reliable textual signals.
Use the exact rule IDs from rules.md (R01, R02, …).

--- OUTPUT FORMAT ---
Output EXACTLY two fenced code blocks, in this order:
1. ```rules.md
   <content>
   ```
2. ```keywords.toml
   <content>
   ```

Do not output anything outside these two fenced blocks.
"""

_SCAFFOLD_PROMPT = """\
Migration: {from_version} → {to_version}

Migration notes and reference material:
---
{sources_text}
---

Generate the rules.md and keywords.toml for this migration profile.
"""


def scaffold_profile(
    name: str,
    from_version: str,
    to_version: str,
    sources: list[str],
    test_command: str = "",
    source_glob: str = "**/*.java",
    target_ext: str = ".java",
    sandbox_image: str = "eclipse-temurin:21",
    e2b_template: str = "base",
) -> None:
    """Generate a new migration profile directory using LLM assistance."""
    from agent_core.models import Tier, complete_with_cost, get_run_budget

    profile_dir = Path(__file__).parent / "profiles" / name
    if profile_dir.exists():
        log.warning("Profile directory %s already exists — overwriting files", profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Gather source material
    sources_text = _fetch_sources(sources)
    if not sources_text.strip():
        sources_text = f"(No additional sources provided — use LLM knowledge about {from_version} → {to_version} migration)"

    prompt = _SCAFFOLD_PROMPT.format(
        from_version=from_version,
        to_version=to_version,
        sources_text=sources_text[:12_000],
    )

    log.info("Calling LLM to scaffold profile '%s' (%s → %s) ...", name, from_version, to_version)
    budget = get_run_budget(f"scaffold-{name}")
    response, cost = complete_with_cost(
        prompt,
        system=_SCAFFOLD_SYSTEM,
        tier=Tier.HIGH,
        max_tokens=4096,
        budget=budget,
    )
    log.info("Scaffold LLM call cost: $%.4f", cost)

    rules_md, keywords_toml = _parse_scaffold_response(response)

    # Write rules.md
    rules_path = profile_dir / "rules.md"
    rules_path.write_text(rules_md)
    log.info("Wrote %s", rules_path)

    # Write keywords.toml
    keywords_path = profile_dir / "keywords.toml"
    keywords_path.write_text(keywords_toml)
    log.info("Wrote %s", keywords_path)

    # Write tests.toml
    tests_toml = _generate_tests_toml(
        test_command=test_command or _guess_test_command(sandbox_image),
        source_glob=source_glob,
        target_ext=target_ext,
        sandbox_image=sandbox_image,
        e2b_template=e2b_template,
    )
    tests_path = profile_dir / "tests.toml"
    tests_path.write_text(tests_toml)
    log.info("Wrote %s", tests_path)

    print(f"\n✅ Profile '{name}' scaffolded at {profile_dir}")
    print(f"   rules.md:     {rules_path}")
    print(f"   keywords.toml:{keywords_path}")
    print(f"   tests.toml:   {tests_path}")
    print("\nReview and adjust the files before running a migration.")


def _fetch_sources(sources: list[str]) -> str:
    """Fetch content from URLs and read local files."""
    parts: list[str] = []
    for src in sources:
        if src.startswith("http://") or src.startswith("https://"):
            content = _fetch_url(src)
        else:
            path = Path(src)
            if path.exists():
                content = path.read_text(errors="replace")
                log.info("Read local file: %s (%d chars)", src, len(content))
            else:
                log.warning("Source not found: %s — skipping", src)
                content = ""
        if content:
            parts.append(f"=== Source: {src} ===\n{content[:4000]}")
    return "\n\n".join(parts)


def _fetch_url(url: str) -> str:
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read(50_000).decode("utf-8", errors="replace")
        # Strip HTML tags roughly
        import re
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s{3,}", "\n\n", text)
        log.info("Fetched URL: %s (%d chars)", url, len(text))
        return text[:6000]
    except Exception as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return ""


def _parse_scaffold_response(text: str) -> tuple[str, str]:
    """Extract rules.md and keywords.toml content from LLM response."""
    import re

    rules_md = ""
    keywords_toml = ""

    rules_match = re.search(r"```rules\.md\n(.*?)```", text, re.DOTALL)
    if rules_match:
        rules_md = rules_match.group(1).strip()

    keywords_match = re.search(r"```keywords\.toml\n(.*?)```", text, re.DOTALL)
    if keywords_match:
        keywords_toml = keywords_match.group(1).strip()

    # Fallback: try generic fenced blocks in order
    if not rules_md or not keywords_toml:
        blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
        if len(blocks) >= 1 and not rules_md:
            rules_md = blocks[0].strip()
        if len(blocks) >= 2 and not keywords_toml:
            keywords_toml = blocks[1].strip()

    if not rules_md:
        rules_md = "# Rules\n\n(LLM did not produce rules — review the raw response)\n"
    if not keywords_toml:
        keywords_toml = "# Keywords\n\n[source_keywords]\n# (LLM did not produce keywords)\n"

    return rules_md, keywords_toml


def _generate_tests_toml(
    test_command: str,
    source_glob: str,
    target_ext: str,
    sandbox_image: str,
    e2b_template: str,
) -> str:
    return textwrap.dedent(f"""\
        [test]
        command = "{test_command}"

        [source]
        glob = "{source_glob}"
        target_ext = "{target_ext}"

        [sandbox]
        image = "{sandbox_image}"
        e2b_template = "{e2b_template}"
        """)


def _guess_test_command(sandbox_image: str) -> str:
    if "maven" in sandbox_image or "mvn" in sandbox_image:
        return "mvn test -B -q"
    return "./gradlew test --rerun-tasks --no-daemon"
