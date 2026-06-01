# code-migration-agent

A **platform** for LLM-driven code migrations. Point it at a repo + a profile
(rules + test command), and it produces a green, reviewed PR.

```
python -m migration.cli \
  --repo https://github.com/org/my-java-app \
  --profile java_to_kotlin \
  --hitl full
```

## How it works

1. **Ingest** — clones the repo into a sandbox, parses all files with tree-sitter
2. **Plan** — builds a dependency graph, topologically sorts files, estimates cost → HITL approval
3. **Worker** — migrates each file using the profile's rules + dep context (LLM Tier.MID)
4. **Verify** — runs the profile's test command in the sandbox after each patch
5. **Fix** — if tests fail, revises the patch (LLM Tier.HARD, bounded retries)
6. **Critic** — LLM judge checks idiomaticity, minimality, behavior-preservation
7. **PR** — opens a GitHub PR with the green diff + Langfuse trace

See [`docs/architecture.md`](docs/architecture.md) for diagrams.

## Why no vector DB?

Migration rules are bounded and map to exact AST patterns — deterministic lookup
is safer and more auditable than fuzzy retrieval. See
[`docs/context-and-retrieval.md`](docs/context-and-retrieval.md).

## Profiles

| Profile | Status |
|---|---|
| `java_to_kotlin` | v1 flagship |
| `junit4_to_junit5` | Phase 5 |
| `spring_boot_2_to_3` | Phase 5 |

## Setup

```bash
uv sync
export ANTHROPIC_API_KEY=sk-...
export GITHUB_TOKEN=ghp_...
```

## HITL levels

| `HITL_LEVEL` | Gates |
|---|---|
| `full` (default) | plan approval · give-up escalation · PR review |
| `plan_only` | plan approval only |
| `none` | fully autonomous (CI / benchmarks) |

## Phases

- **Phase 0** ✅ — scaffold (this state)
- **Phase 1** — tree-sitter parsing + dependency graph
- **Phase 2** — LangGraph core loop + planner
- **Phase 3** — Sandbox safety (Docker / E2B)
- **Phase 4** — eval scorecard + CI gate
- **Phase 5** — more profiles + optional embeddings
