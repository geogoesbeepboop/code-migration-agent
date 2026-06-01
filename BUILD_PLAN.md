# code-migration-agent — Build Plan

## Architecture (three layers)

```
agent-core  (substrate — reusable across all projects)
├── model providers        # Anthropic + OpenAI, Tier routing
├── tracing                # Langfuse
├── evals                  # EvalRunner framework (metrics are per-project)
├── queueing               # durable SQLite task queue
├── budgets                # cost circuit breaker + retries
└── sandbox abstraction    # Sandbox Protocol (Docker/E2B impls)

migration-agent  (the platform)
├── LangGraph workflow     # the orchestrated inner loop
├── tree-sitter parsing    # AST per source file
├── dependency graph       # exact import/call/type edges → migration order
├── migration planner      # builds + explains the plan (HITL-approved)
├── HITL checkpoints       # plan approval · give-up escalation · PR review
├── test runner            # uses agent-core Sandbox to run the profile's tests
└── PR generator           # opens a reviewed PR with the green diff + trace

Profiles  (declarative — the only thing that changes per migration)
├── Java → Kotlin          # v1 flagship (shared JUnit suite as oracle)
├── JUnit4 → JUnit5
├── Spring Boot 2 → 3      # javax → jakarta
└── Future migrations
```

**A Profile = `{ rules, test command, (optional) corpus }`.** The engine never
changes; only the profile + the test-runner's oracle do. That's what makes this
a *platform*, not a one-off.

---

## What it does

Point it at a repo + a profile. It clones into a **Sandbox**, builds the
**dependency graph**, generates a **migration plan** → **[HITL: approve plan]**
→ migrates file-by-file in dependency order, runs the tests after each change,
reads failures and **fixes itself** (bounded retries) → on repeated failure
**[HITL: escalate]** → a **critic** checks the diff → **[HITL: review PR]** →
opens a PR with green tests + a Langfuse trace → **waits for human input** on
whether to keep iterating.

Inner loop = **LangGraph** (explicit nodes, shared state, checkpointing,
interrupts). Substrate = **agent-core**.

> **No vector DB in v1.** Migration knowledge is a *bounded, structured* ruleset
> (loaded by AST pattern) + the *precise* dependency graph — not a fuzzy corpus.
> See `docs/context-and-retrieval.md` for the full reasoning and when (Phase 5+)
> embeddings actually earn their place.

---

## Stack

| Concern | Library |
|---|---|
| Orchestration | `langgraph` |
| Model access / budget / tracing / sandbox / evals | `agent-core[anthropic,docker]` (local editable) |
| Code parsing | `tree-sitter`, `tree-sitter-java`, `tree-sitter-kotlin` |
| Sandbox runtime | `docker` (local dev) · `e2b` (CI) |
| Git / GitHub | `gitpython`, `PyGithub` |
| Test oracle | the profile's own command (`gradle test`, `pytest`) |
| ~~Embeddings / vector DB~~ | **Not in v1** — deferred to Phase 5+ if justified |

---

## HITL gates (mimic production: enough to trust, not enough to nag)

Three gates, each toggleable via `HITL_LEVEL` env (`full` | `plan_only` | `none`)
so you can **remove them as confidence grows**:

1. **Plan approval** — after the planner builds the migration DAG, before *any*
   code changes. You approve scope/order. (Highest-value gate; keep longest.)
2. **Give-up escalation** — when a file exhausts its fix-retry budget. Human
   chooses: skip file / hand-fix / abort run. (Mirrors a real on-call handoff.)
3. **PR review** — after the critic, before the PR is finalized. You approve the
   diff. (Becomes the `PR acceptance rate` metric below.)

NOT gated: individual file migrations that pass on their own. That would be nagging.

Implemented with LangGraph `interrupt_before=[...]`; resume with `graph.invoke(None, config)`.

---

## Evals — a scorecard, not just pass/fail

`pytest`-green is the *oracle*, but a single bool is a weak story. Track a
**scorecard** per file and per repo (logged to Langfuse, gated in CI):

| Metric | What it tells a reviewer |
|---|---|
| **Migration success rate** | % files (and % repos) reaching green tests |
| **Retry count** | avg fix-loop iterations — efficiency of self-correction |
| **Tokens consumed / cost (USD)** | per file + per repo — unit economics |
| **Human interventions** | which HITL gates fired, how often — autonomy level |
| **Files changed / diff size** | scope discipline (smaller = more trustworthy) |
| **PR acceptance rate** | the north star — of generated PRs, % a human merges as-is |

CI gate: fail the build if **migration success rate** regresses vs. the last run.
Benchmark target: beat IntelliJ J2K (Kotlin) and `bump-pydantic` (if added) on
success rate *or* diff-minimality.

---

## Phases

### Phase 0 — Scaffold
```
src/migration/
├── graph.py            # LangGraph definition (the spine)
├── state.py            # TypedDict shared state
├── nodes/              # ingest · plan · worker · verify · fix · critic · pr
├── depgraph.py         # tree-sitter → dependency graph
├── test_runner.py      # Verifier: agent-core Sandbox + profile test cmd
├── hitl.py             # gate helpers + HITL_LEVEL handling
└── profiles/
    └── java_to_kotlin/
        ├── rules.md     # breaking-change catalog (structured, AST-keyed)
        └── tests.toml   # how to run the suite ("./gradlew test ...")
evals/
├── runner.py           # EvalRunner impl → the scorecard above
├── corpus/             # 8–15 public Java repos w/ real test suites
└── baselines/          # IntelliJ J2K outputs (to beat)
```
**Verify:** `python -c "from migration.graph import build_graph; print('ok')"`

### Phase 1 — Parsing & Dependency Graph 
*Concept (ask `tutor`): ASTs, tree-sitter, topological sort.*
- `depgraph.py` — parse every source file with tree-sitter; extract imports,
  class/method names, call + type-reference edges; build a directed graph.
- Topologically sort it (leaf modules first) → this *is* the migration order.
- Load the profile's `rules.md` into a structured rule index, keyed by the AST
  patterns each rule applies to (e.g. import `javax.persistence` → jakarta rule).
**Verify:** point it at a small Java repo → print the migration order + which
rules each file triggers. No model calls needed yet.
**ADR:** why structured rule-lookup + dep-graph beats RAG here.

### Phase 2 — LangGraph Core Loop + Planner 
*Concept (ask `tutor`): StateGraph, conditional edges, checkpointing, interrupt.*
```python
# graph.py — the spine
g = StateGraph(MigrationState)
for name, fn in [("ingest", ingest), ("plan", plan), ("worker", worker),
                 ("verify", verify), ("fix", fix), ("critic", critic), ("pr", pr)]:
    g.add_node(name, fn)

g.set_entry_point("ingest")
g.add_edge("ingest", "plan")
g.add_edge("plan", "worker")                       # interrupt_before=["worker"] = plan-approval gate
g.add_edge("worker", "verify")
g.add_conditional_edges("verify", route_after_verify, {
    "pass": "critic", "fix": "fix", "give_up": "critic",  # give_up still goes to critic→escalation
})
g.add_edge("fix", "verify")
g.add_edge("critic", "pr")
g.add_edge("pr", END)

return g.compile(
    checkpointer=SqliteSaver.from_conn_string(".agent-core/checkpoints.db"),
    interrupt_before=hitl_gates(),   # ["worker","critic","pr"] at HITL_LEVEL=full
)
```
- **planner** builds the per-file task DAG *and a human-readable plan summary*
  (what changes, in what order, est. cost) for the approval gate.
- **worker** loads the file's triggered rules + dep-graph context, calls
  `agent_core.complete(Tier.MID)`, emits a unified diff.
- **verify** = `test_runner` runs the profile's test command in the Sandbox,
  parses pass/fail + failure text.
- **fix** (`Tier.HARD`) reads the failure, revises the patch; `fix_attempts++`;
  route to `give_up` after N.
- **critic** LLM-judge: idiomatic? minimal? behavior-preserving?
**Verify:** full run on one small repo → plan gate pauses → approve → file
migrates to green → PR gate pauses.

### Phase 3 — Sandbox & Safety
- Use agent-core's `Sandbox` Protocol; impl `DockerSandbox` (local) +
  `E2BSandbox` (CI). Generated code runs ONLY in the sandbox.
- Patches applied as git commits inside the sandbox → fully revertable.
- `AGENT_CORE_MAX_USD_PER_TASK` caps spend per run.
**Verify:** good patch → tests pass in sandbox; broken patch → structured
failure the fix node can read.

### Phase 4 — Eval Scorecard + CI 
- Build the corpus (8–15 public Java repos, 100+ tests each).
- `evals/runner.py` produces the full scorecard; run IntelliJ J2K for the baseline.
- CI gate on migration-success-rate regression; post the scorecard as a PR comment.
**Verify:** scorecard table beats the J2K baseline on ≥1 metric.
**ADR:** eval methodology + why these metrics.

### Phase 5 — More Profiles + (optional) Retrieval
- Add **JUnit4→JUnit5** and **Spring Boot 2→3** profiles (new rules + test cmd;
  the graph is untouched — proves the platform claim).
- **Only if a repo exceeds the context window OR you want the agent to learn
  from past migrations:** add pgvector (Supabase) for (a) semantic code
  retrieval, (b) a library of accepted before/after patches as few-shot
  examples. This is where embeddings finally earn their place — see
  `docs/context-and-retrieval.md`.
- Deep-dive artifacts: `docs/system-design.md`, ADRs, `docs/benchmark.md`,
  10–15 min recorded walkthrough (plan→fail→self-fix→pass→trace).
