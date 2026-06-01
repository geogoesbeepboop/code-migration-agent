"""Parse a profile's rules.md into a structured rule index keyed by AST patterns.

Each rule in rules.md has the form:
    ## R<n> — <title>
    **Pattern:** <pattern description>

The rule index maps pattern keywords (import paths, annotation names, type names)
to the list of rule IDs that should fire when that pattern appears in a file.

Keywords are loaded from a per-profile ``keywords.toml`` when present, falling
back to the hardcoded maps below for backward compatibility.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Rule:
    rule_id: str       # e.g. "R01"
    title: str         # e.g. "Null safety: field declarations"
    pattern: str       # raw pattern text from the rules file
    transform: str     # raw transform description
    keywords: list[str] = field(default_factory=list)


@dataclass
class RuleIndex:
    rules: list[Rule]
    _keyword_map: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for rule in self.rules:
            for kw in rule.keywords:
                self._keyword_map.setdefault(kw.lower(), []).append(rule.rule_id)

    def rules_for_file(self, imports: list[str], source: str) -> list[str]:
        """Return rule IDs that fire for a file with the given imports and source text."""
        fired: dict[str, bool] = {}
        src_lower = source.lower()

        for imp in imports:
            # match full import path or any suffix
            for kw, rule_ids in self._keyword_map.items():
                if kw in imp.lower():
                    for rid in rule_ids:
                        fired[rid] = True

        for kw, rule_ids in self._keyword_map.items():
            if kw in src_lower:
                for rid in rule_ids:
                    fired[rid] = True

        # return in rule-document order
        id_order = {r.rule_id: i for i, r in enumerate(self.rules)}
        return sorted(fired.keys(), key=lambda r: id_order.get(r, 999))

    def rule_text(self, rule_ids: list[str]) -> str:
        """Render the full rule text for the given rule IDs (for LLM prompt injection)."""
        id_to_rule = {r.rule_id: r for r in self.rules}
        lines: list[str] = []
        for rid in rule_ids:
            rule = id_to_rule.get(rid)
            if rule:
                lines.append(f"### {rule.rule_id} — {rule.title}")
                lines.append(f"Pattern: {rule.pattern}")
                lines.append(f"Transform: {rule.transform}")
                lines.append("")
        return "\n".join(lines)


# ── patterns that we scan source text for, per rule ────────────────────────────

_RULE_KEYWORDS: dict[str, list[str]] = {
    "R01": ["= null", "nullable", "@nullable"],
    "R02": ["@data", "@lombok.data", "getters", "setters", "hashcode", "tostring"],
    "R03": ["final ", "var ", "local variable"],
    "R04": ["string.format", '+ "', '" +'],
    "R05": ["switch (", "switch("],
    "R06": ["public static"],
    "R07": ["static ", "static final"],
    "R08": ["completablefuture", "executorservice", "@async"],
    "R09": ["@jvmfield", "@jvmstatic", "@jvmoverloads"],
    "R10": ["throws ", "checked exception"],
    "R11": ["collections.unmodifiablelist", "arrays.aslist", "new arraylist"],
    "R12": ["!= null", "== null", "if (null"],
    "R13": ["@autowired"],
}

_IMPORT_KEYWORDS: dict[str, list[str]] = {
    "R01": ["javax.validation.constraints.notnull", "org.jetbrains.annotations.nullable"],
    "R02": ["lombok.data", "lombok.getter", "lombok.setter"],
    "R08": ["java.util.concurrent.completablefuture", "java.util.concurrent.executorservice",
            "org.springframework.scheduling.annotation.async"],
    "R11": ["java.util.collections", "java.util.arrays", "java.util.arraylist"],
    "R13": ["org.springframework.beans.factory.annotation.autowired"],
}


def load_rule_index(rules_path: Path, keywords_path: Path | None = None) -> RuleIndex:
    """Parse rules.md and build a RuleIndex.

    If ``keywords_path`` points to a valid ``keywords.toml``, its contents
    override the hardcoded keyword maps. This allows each profile to declare its
    own keyword patterns without touching rule_loader.py.
    """
    text = rules_path.read_text()
    rules = _parse_rules_md(text)

    if keywords_path and keywords_path.exists():
        _attach_keywords_from_toml(rules, keywords_path)
    else:
        # Backward-compatible fallback
        _attach_keywords(rules)

    return RuleIndex(rules=rules)


def _parse_rules_md(text: str) -> list[Rule]:
    rule_blocks = re.split(r"\n---\n", text)
    rules: list[Rule] = []

    for block in rule_blocks:
        # Look for ## R<n> — <title>
        header = re.search(r"##\s+(R\d+)\s+[—–-]+\s+(.+)", block)
        if not header:
            continue
        rule_id = header.group(1)
        title = header.group(2).strip()

        pattern_m = re.search(r"\*\*Pattern:\*\*\s*(.+?)(?=\n\*\*|\Z)", block, re.DOTALL)
        transform_m = re.search(r"\*\*Transform:\*\*\s*(.+?)(?=\n\*\*|\Z)", block, re.DOTALL)

        pattern = pattern_m.group(1).strip() if pattern_m else ""
        transform = transform_m.group(1).strip() if transform_m else ""

        rules.append(Rule(rule_id=rule_id, title=title, pattern=pattern, transform=transform))

    return rules


def _attach_keywords(rules: list[Rule]) -> None:
    id_to_rule = {r.rule_id: r for r in rules}
    for rule_id, kws in _RULE_KEYWORDS.items():
        rule = id_to_rule.get(rule_id)
        if rule:
            rule.keywords.extend(kws)
    for rule_id, kws in _IMPORT_KEYWORDS.items():
        rule = id_to_rule.get(rule_id)
        if rule:
            rule.keywords.extend(kws)


def _attach_keywords_from_toml(rules: list[Rule], keywords_path: Path) -> None:
    """Load keywords from a profile's keywords.toml and attach to rules.

    Expected format:
        [source_keywords]
        R01 = ["= null", "@nullable"]

        [import_keywords]
        R01 = ["javax.validation.constraints.notnull"]
    """
    with open(keywords_path, "rb") as f:
        data = tomllib.load(f)

    id_to_rule = {r.rule_id: r for r in rules}

    for rule_id, kws in data.get("source_keywords", {}).items():
        rule = id_to_rule.get(rule_id)
        if rule and isinstance(kws, list):
            rule.keywords.extend(kws)

    for rule_id, kws in data.get("import_keywords", {}).items():
        rule = id_to_rule.get(rule_id)
        if rule and isinstance(kws, list):
            rule.keywords.extend(kws)
