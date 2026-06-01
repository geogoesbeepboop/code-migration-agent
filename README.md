# code-migration-agent

A **platform** for LLM-driven code migrations. Point it at a repo + a profile
(rules + test command), and it produces a green, reviewed PR.

```bash
python -m migration.cli migrate \
  --repo https://github.com/org/my-java-app \
  --profile java_to_kotlin \
  --hitl full
```

The CLI stays alive through all approval gates — no need to run a separate `--resume` command.

## How it works

1. **Ingest** — clones the repo into a sandbox, parses all files with tree-sitter, builds a dependency DAG
2. **Plan** — topologically sorts files (leaf-first), estimates cost → inline HITL approval (y/n)
3. **Worker** — migrates each file using the profile's rules + dep context (LLM Tier.MID)
4. **Verify** — runs the profile's test command in the sandbox after each patch
5. **Fix** — if tests fail, revises the patch (LLM Tier.HARD, bounded retries)
6. **Critic** — LLM judge receives the **original source + migrated source + diff** to check idiomaticity, minimality, behavior-preservation
7. **Resolve give-ups** — all files that exhausted retries are shown together in a single grouped gate before the PR
8. **PR** — LLM writes a narrative PR description; pushes branch and opens a GitHub PR with a Langfuse trace URL

See [`docs/architecture.md`](docs/architecture.md) for diagrams.

## Why no vector DB?

Migration rules are bounded and map to exact AST patterns — deterministic lookup
is safer and more auditable than fuzzy retrieval. See
[`docs/context-and-retrieval.md`](docs/context-and-retrieval.md).

## Profiles

| Profile | Description | Build tool | Status |
|---|---|---|---|
| `java_to_kotlin` | Java → Kotlin | Gradle | ✅ v1 flagship |
| `java_to_kotlin_maven` | Java → Kotlin | Maven | ✅ |
| `spring_boot_2_to_3` | Spring Boot 2.x → 3.x (javax→jakarta, Security 6, etc.) | Gradle | ✅ |
| `spring_boot_2_to_3_maven` | Spring Boot 2.x → 3.x | Maven | ✅ |
| `junit4_to_junit5` | JUnit 4 → JUnit 5 | Any | Phase 6 |

## Scaffold a new profile

Use the LLM to generate a new profile from version notes or migration guides:

```bash
python -m migration.cli scaffold-profile \
  --name my_custom_migration \
  --from "Framework 1.x" \
  --to "Framework 2.x" \
  --sources https://framework.io/migration-guide.html path/to/local-notes.txt \
  --test-command "./gradlew test" \
  --sandbox-image "eclipse-temurin:21"
```

This creates `src/migration/profiles/my_custom_migration/` with `rules.md`,
`keywords.toml`, and `tests.toml`. Review and adjust before running a migration.

Each profile consists of:
- `rules.md` — numbered migration rules (Pattern + Transform per rule)
- `keywords.toml` — source text and import patterns to match rules to files
- `tests.toml` — test command, source glob, Docker sandbox image

Profiles support inheritance: set `inherits = "parent_profile"` in `tests.toml`
to share `rules.md` and `keywords.toml` across build-tool variants.

## Setup

```bash
uv sync
export ANTHROPIC_API_KEY=sk-...
export GITHUB_TOKEN=ghp_...

# Optional: Langfuse tracing (Phase 5)
export LANGFUSE_PUBLIC_KEY=pk-...
export LANGFUSE_SECRET_KEY=sk-...
```

## HITL gates

The CLI prompts you inline at each gate — no need to rerun with `--resume`.
At each gate you can type `y` to approve or `n` to provide feedback and have
the agent redo that step.

| Gate | When | On "n" |
|---|---|---|
| **Gate 1** — Plan approval | Before first file | Feedback incorporated; plan regenerated |
| **Gate 2** — Give-up review | After all files (deferred, default) or per-file (immediate) | Instructions injected; file retried from scratch |
| **Gate 3** — PR review | Before PR is opened | Feedback used to rewrite PR description |

```
HITL_LEVEL   Gates
full         plan approval · give-up review · PR review  (default)
plan_only    plan approval only
none         fully autonomous (CI / benchmarks)

--hitl-gate2 deferred   All failures grouped in one gate before PR  (default)
--hitl-gate2 immediate  Each failure interrupts immediately (original behaviour)
```

## Langfuse tracing (Phase 5)

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` to enable tracing.
Each run creates a trace with per-node spans. The trace URL is embedded in the PR body.
Tracing is a graceful no-op if keys are not set.

## Phases

- **Phase 0** ✅ — scaffold
- **Phase 1** ✅ — tree-sitter parsing + dependency graph
- **Phase 2** ✅ — LangGraph core loop + planner
- **Phase 3** ✅ — Sandbox safety (Docker / E2B)
- **Phase 4** ✅ — eval scorecard + CI gate
- **Phase 5** ✅ — Langfuse tracing · Spring Boot 2→3 · Maven profiles · inline HITL · profile scaffold
