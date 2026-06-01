# code-migration-agent — Architecture

## System Overview

This is a **platform**, not a one-off tool. The engine stays constant; only the
*profile* (rules + test command) changes per migration type.

```
┌──────────────────────────────────────────────┐
│  Profiles  (declarative, one per migration)  │
│  Java→Kotlin  ·  JUnit4→5  ·  Spring 2→3    │
└────────────────────┬─────────────────────────┘
                     │ rules.md + tests.toml
┌────────────────────▼─────────────────────────┐
│  migration-agent  (the orchestrated platform) │
│  LangGraph workflow  ·  tree-sitter parsing   │
│  dependency graph  ·  HITL gates  ·  PR gen  │
└────────────────────┬─────────────────────────┘
                     │ model / tracing / sandbox / budget
┌────────────────────▼─────────────────────────┐
│  agent-core  (substrate — reusable)           │
│  Anthropic + OpenAI  ·  Langfuse  ·  Evals   │
│  SQLite task queue  ·  Sandbox Protocol       │
└──────────────────────────────────────────────┘
```

---

## LangGraph Workflow

The inner loop as a directed graph. Nodes are Python functions; edges are
deterministic or conditional. Checkpointing happens at every node boundary.

```mermaid
flowchart TD
    START([START]) --> ingest

    ingest["ingest\n— clone repo into Sandbox\n— tree-sitter parse all files\n— build dep graph + rule index"]
    plan["plan\n— format migration plan\n— estimate cost per file"]
    plan_review["plan_review\n— pass-through\n— HITL gate 1 interrupt point"]
    worker["worker\n— load file rules + dep context\n— call LLM Tier.MID (Sonnet)\n— emit unified diff"]
    verify["verify\n— git apply patch\n— run profile test command\n— parse pass/fail"]
    fix["fix\n— read failure text\n— call LLM Tier.HARD (Opus)\n— revise patch · fix_attempts++"]
    critic["critic\n— LLM judge: idiomatic? minimal?\n— behavior-preserving?\n— HITL gate 2 if gave_up"]
    next_file["next_file\n— git commit accepted patch\n— track gave_up files\n— advance current_file_index"]
    pr["pr\n— push branch to GitHub\n— open PR with trace URL\n— HITL gate 3 interrupt point"]
    END_NODE([END])

    ingest --> plan
    plan --> plan_review
    plan_review --> worker
    worker --> verify
    verify -->|pass| critic
    verify -->|fix| fix
    verify -->|give_up| critic
    fix --> verify
    critic --> next_file
    next_file -->|more files| worker
    next_file -->|all done| pr
    pr --> END_NODE

    style plan_review fill:#fef3c7,stroke:#d97706
    style critic fill:#fef3c7,stroke:#d97706
    style pr fill:#fef3c7,stroke:#d97706
    style mark_give_up fill:#fee2e2,stroke:#dc2626
```

**Yellow nodes** = HITL interrupt points at `HITL_LEVEL=full`.
**Red node** = `mark_give_up` sets `current_file_gave_up=True` before `critic` raises HITL gate 2.
`plan_review` and `pr` use static `interrupt_before=[]`; `critic` raises
`NodeInterrupt` dynamically when a file exhausts retries (gate 2).

---

## HITL Gates

Three toggleable checkpoints. Each maps to a `interrupt_before` node in LangGraph.

```mermaid
sequenceDiagram
    participant Agent
    participant Human

    Agent->>Agent: ingest + build dep graph
    Agent->>Agent: plan (DAG + cost estimate)
    Agent-->>Human: 🛑 GATE 1: approve plan?
    Human-->>Agent: ✅ approved (or ❌ abort)

    loop for each file in migration order
        Agent->>Agent: worker (migrate file)
        Agent->>Agent: verify (run tests)
        alt tests pass
            Agent->>Agent: (continue)
        else retry budget exhausted
            Agent-->>Human: 🛑 GATE 2: skip / hand-fix / abort?
            Human-->>Agent: decision
        end
    end

    Agent->>Agent: critic (review full diff)
    Agent-->>Human: 🛑 GATE 3: approve PR?
    Human-->>Agent: ✅ approved
    Agent->>Agent: open PR
```

`HITL_LEVEL` env var controls which gates fire:

| Level | Gates active |
|---|---|
| `full` | plan approval · give-up escalation · PR review |
| `plan_only` | plan approval only |
| `none` | fully autonomous (CI / benchmarking) |

---

## Dependency Graph & Migration Order

tree-sitter parses every source file into an AST. We extract edges, build a
directed graph, and topologically sort it so leaf modules migrate first.

```mermaid
flowchart LR
    subgraph Java repo
        A[DatabaseConfig.java\nimports: javax.persistence]
        B[UserRepository.java\nimports: A, javax.persistence]
        C[UserService.java\nimports: B]
        D[UserController.java\nimports: C]
    end

    A -->|depended on by| B
    B -->|depended on by| C
    C -->|depended on by| D

    subgraph Migration order topo sort
        direction TB
        O1[1. DatabaseConfig]
        O2[2. UserRepository]
        O3[3. UserService]
        O4[4. UserController]
        O1 --> O2 --> O3 --> O4
    end
```

Each file also gets a **rule index** — the set of breaking-change rules triggered
by its AST patterns (e.g. `javax.persistence` import → `javax→jakarta` rule).

---

## Data Flow

How information moves through a single file migration:

```mermaid
flowchart LR
    repo[Git Repo<br/>(Sandbox clone)] --> ts[tree-sitter parser]

    ts --> depgraph[Dependency Graph]
    ts --> ruleidx[Rule Index<br/>(AST-keyed)]

    depgraph --> order[Migration Order]
    ruleidx --> worker
    order --> worker

    worker[worker node<br/>(LLM Tier MID)] --> patch[Unified Diff]

    patch --> verify[verify node<br/>(Sandbox + test cmd)]

    verify -->|pass| critic[critic node<br/>(LLM judge)]
    verify -->|fail| fix[fix node<br/>(LLM Tier HARD)]

    fix --> verify
    critic --> pr[pr node<br/>(GitHub API)]

    pr --> github[GitHub PR<br/>+ Langfuse trace]
```

---

## Eval Scorecard

Every run emits a scorecard logged to Langfuse and gated in CI.

| Metric | Signal |
|---|---|
| Migration success rate | % files / repos reaching green tests |
| Retry count | avg fix-loop iterations (efficiency) |
| Tokens / cost (USD) | per file + per repo (unit economics) |
| Human interventions | which HITL gates fired, how often |
| Files changed / diff size | scope discipline |
| **PR acceptance rate** | **north star** — % of generated PRs a human merges as-is |

CI fails if `migration_success_rate` regresses vs. the last run.

---

## Phase Roadmap

```mermaid
gantt
    title Build Phases
    dateFormat  YYYY-MM-DD
    section Foundation
    Phase 0 — Scaffold            :done,  p0, 2026-05-31, 2d
    Phase 1 — Parsing + DepGraph  :done,  p1, after p0,   3d
    Phase 2 — LangGraph Core Loop :done,  p2, after p1,   4d
    section Safety + Validation
    Phase 3 — Sandbox + Safety    :done,   p3, after p2,  3d
    Phase 4 — Eval Scorecard + CI :done,   p4, after p3,  3d
    section Platform
    Phase 5 — More Profiles       :        p5, after p4,  5d
```

## Implementation Status

| Component | File | Phase | Status |
|---|---|---|---|
| Dep graph (tree-sitter) | `depgraph.py` | 1 | ✅ |
| Rule loader | `rule_loader.py` | 1 | ✅ |
| ingest node | `nodes/ingest.py` | 1 | ✅ |
| plan node | `nodes/plan.py` | 1 | ✅ |
| plan_review node | `nodes/plan_review.py` | 2 | ✅ |
| worker node (LLM Tier.MID) | `nodes/worker.py` | 2 | ✅ |
| verify node (git apply + tests) | `nodes/verify.py` | 2 | ✅ (LocalSandbox) |
| fix node (LLM Tier.HARD) | `nodes/fix.py` | 2 | ✅ |
| critic node (LLM judge) | `nodes/critic.py` | 2 | ✅ |
| next_file node (commit + route) | `nodes/next_file.py` | 2 | ✅ |
| pr node (PyGithub) | `nodes/pr.py` | 2 | ✅ |
| agent-core model tiers | `agent_core/models.py` | 2 | ✅ |
| agent-core LocalSandbox | `agent_core/sandbox.py` | 2 | ✅ |
| agent-core DockerSandbox | `agent_core/sandbox.py` | 3 | ✅ |
| agent-core E2BSandbox | `agent_core/sandbox.py` | 3 | ✅ |
| Budget circuit breaker | `agent_core/models.py` | 3 | ✅ |
| Sandbox ↔ verify/ingest | `nodes/verify.py`, `nodes/ingest.py` | 3 | ✅ |
| Eval scorecard + CI | `evals/runner.py` | 4 | ✅ |
| Corpus manifest | `evals/corpus/manifest.toml` | 4 | ✅ (needs repo verification) |
| CI workflows | `.github/workflows/evals*.yml` | 4 | ✅ |
| ADR eval methodology | `docs/adr/001-eval-methodology.md` | 4 | ✅ |
| mark_give_up node | `nodes/verify.py` | 4 | ✅ |
| FileEvalRecord in state | `state.py` | 4 | ✅ |
| Langfuse tracing | — | 5 | ⬜ |
| JUnit4→5 profile | `profiles/junit4_to_junit5/` | 5 | ⬜ |
| Spring Boot 2→3 profile | `profiles/spring_2_to_3/` | 5 | ⬜ |

---

## Phase 3 — Sandbox Isolation Model

```mermaid
flowchart LR
    subgraph HOST ["Host machine"]
        repo["Repo\n(temp dir)"]
        git["git apply\ngit commit\ngit push"]
    end

    subgraph DOCKER ["Docker container (or E2B sandbox)"]
        tests["./gradlew test\n(or profile test command)"]
        mount["/workspace\n(volume mount)"]
    end

    subgraph PYTHON ["Migration agent process"]
        verify["verify node"]
        ingest["ingest node"]
    end

    ingest -->|"clone + start container"| DOCKER
    ingest -->|"clone"| repo
    repo <-->|"volume mount"| mount
    verify -->|"git apply (host)"| git
    git --> repo
    verify -->|"run test command"| tests
    tests --> verify
```

**Split execution model:**
- `git apply`, `git reset`, `git commit` → run on HOST (fast, no Docker overhead)
- Test commands → run INSIDE container (isolated, resource-limited)
- Volume mount means patched files are instantly visible to the container — no copying

**Sandbox backends:**

| Backend | `SANDBOX_BACKEND` | Used for | Requirements |
|---|---|---|---|
| `LocalSandbox` | `local` (default) | Dev / unit tests | None |
| `DockerSandbox` | `docker` | Local dev | Docker Desktop running |
| `E2BSandbox` | `e2b` | CI | `E2B_API_KEY` env var |

**DockerSandbox resource limits (configurable via env vars):**

| Limit | Env var | Default |
|---|---|---|
| Memory | `SANDBOX_MEM_LIMIT` | `4g` |
| CPU quota | `SANDBOX_CPU_QUOTA` | `100000` (1 CPU) |
| Test timeout | `TEST_TIMEOUT_SECONDS` | `300` |

**Budget circuit breaker:**
- `AGENT_CORE_MAX_USD_PER_TASK` caps total LLM spend per run (default `$5.00`)
- Charged after every `complete()` call using Anthropic response `usage` tokens
- `BudgetExceededError` raised mid-run if limit hit; LangGraph checkpoint preserves state

## Key Design Decisions

- **No vector DB in v1** — migration rules are bounded + AST-keyed (exact lookup
  beats fuzzy retrieval for safety-critical transforms). See
  `context-and-retrieval.md` for the full argument.
- **LangGraph over raw loops** — explicit nodes, shared state, checkpointing, and
  `interrupt_before` make HITL gates first-class, not bolted on.
- **Profile = rules.md + tests.toml** — the engine never changes; only the profile
  does. Proves this is a platform, not a one-off.
- **Sandbox isolation** — all generated code runs in Docker (local) or E2B (CI).
  Patches are git commits inside the sandbox — fully revertable.
