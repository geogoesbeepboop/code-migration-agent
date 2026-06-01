"""Migration profiles — the only thing that changes per migration type.

A Profile is fully declarative:  rules.md + tests.toml.
The migration engine reads it; nothing in the engine changes per profile.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Profile:
    name: str
    rules_path: Path
    test_command: str
    source_glob: str        # e.g. "**/*.java"
    target_ext: str         # e.g. ".kt"
    sandbox_image: str      # Docker image for local dev
    e2b_template: str       # E2B template name for CI

    def load_rules_text(self) -> str:
        return self.rules_path.read_text()


def load_profile(name: str) -> Profile:
    """Load a profile by name from src/migration/profiles/<name>/tests.toml."""
    profile_dir = Path(__file__).parent / name
    if not profile_dir.exists():
        available = _list_profiles()
        raise ValueError(f"Unknown profile: {name!r}. Available: {available}")

    with open(profile_dir / "tests.toml", "rb") as f:
        config = tomllib.load(f)

    return Profile(
        name=name,
        rules_path=profile_dir / "rules.md",
        test_command=config["test"]["command"],
        source_glob=config["source"]["glob"],
        target_ext=config["source"]["target_ext"],
        sandbox_image=config.get("sandbox", {}).get("image", "gradle:8-jdk21"),
        e2b_template=config.get("sandbox", {}).get("e2b_template", "base"),
    )


def _list_profiles() -> list[str]:
    return [
        d.name
        for d in Path(__file__).parent.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    ]
