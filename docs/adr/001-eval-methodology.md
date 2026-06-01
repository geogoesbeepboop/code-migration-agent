# ADR 001 — Eval Methodology for the Migration Agent

**Status:** Accepted  
**Date:** 2026-05-31  
**Deciders:** George Andrade

---

## Context

We need a rigorous, reproducible way to measure whether the migration agent is
improving or regressing. A single boolean ("did CI pass?") is too coarse: it
hides *how well* files migrated, whether the agent is getting expensive, and
whether humans are being interrupted unnecessarily. We also need a comparison
target — the obvious baseline is **IntelliJ IDEA's built-in J2K (Java-to-Kotlin)
converter**, the industry benchmark for this task.

---

## Decision

### 1. The scorecard (what we measure)

| Metric | Why it matters | Source in state |
|---|---|---|
| **Migration success rate** | Primary quality signal: % files reaching green tests | `file_eval_records[].success` |
| **Give-up rate** | % files that exhausted retries → measures autonomy gap | `file_eval_records[].gave_up` |
| **Avg fix-loop iterations** | Efficiency: how hard the agent has to work | `file_eval_records[].fix_attempts` |
| **Tokens / cost per file** | Unit economics: what does one migration cost? | `file_eval_records[].cost_usd` |
| **Diff size (changed lines)** | Scope discipline: smaller = more trustworthy, easier review | `file_eval_records[].diff_size_lines` |
| **PR acceptance rate** | North-star: do humans merge our PRs as-is? | Manual tracking (Langfuse) |
| **Human interventions** | How often does HITL fire? Measures autonomy level | `state.human_interventions` |

### 2. The CI gate (what blocks a merge)

A PR is blocked if **migration success rate regresses more than 1 percentage
point vs. the stored baseline**. The 1pp tolerance absorbs random variation in
test flakiness across the corpus.

We do NOT gate on cost or fix-loop counts because these fluctuate legitimately
as we tune prompts. They are tracked and trending over time, not gated.

### 3. The corpus (what we run against)

8 public Java+Gradle repos covering:
- Pure-logic algorithms (stress tests R03/R04/R05 — val/var, string templates, switch)
- Library code (stress tests R01/R12 — null handling)
- Builder patterns (R06/R07 — extension functions, companion objects)
- Async code (R08 — coroutines, flagged for human review)

We exclude Maven-based repos from v1 because our profile only targets Gradle
(`./gradlew test`). Maven support is a separate profile in Phase 5.

**Corpus size trade-off:** 8 repos is enough to get meaningful signal without
$50+ per eval run. Each run costs roughly $0.50–$2.00 at Sonnet rates across a
small corpus. Full corpus runs are nightly; PR CI runs only the fast subset
(1–2 repos).

### 4. The baseline (what we compare against)

**IntelliJ J2K** (the Kotlin plugin's Java-to-Kotlin converter) is the obvious
benchmark:
- It's free, widely used, and well-understood
- It converts at the file level (same granularity as our agent)
- It does NOT run tests after conversion, so its "success rate" requires manual verification

**How to measure the J2K baseline:**
1. Clone each corpus repo
2. Run IntelliJ J2K via headless CLI: `idea convert-java-to-kotlin <file>`
3. Run `./gradlew test` after each file conversion
4. Record pass/fail per file → `overall_success_rate`

This process is manual today. A future script can automate it via the IntelliJ
Platform Plugin SDK. For now, the baseline JSON is a stub set to `0.0`; update
it after the first manual J2K run.

### 5. The north-star metric

**PR acceptance rate** — of PRs the agent opens, what % does a human merge
without requesting changes?

This is the real-world outcome metric. `migration_success_rate` tells you
about automated tests; `PR acceptance rate` tells you whether a human would
trust the output. We track it manually via Langfuse (Phase 4 adds tracing).

Target: **beat J2K on success rate OR diff-minimality on the corpus**.

---

## Alternatives considered

### "Just use pytest pass/fail as the only metric"
Rejected. A single bool hides: cost regressions, efficiency regressions, and
whether the agent is doing too much (over-migrating). The scorecard is cheap to
collect and materially richer.

### "Gate on every metric (cost, fix-loops, etc.)"
Rejected. Gating on cost and fix-loop counts would block legitimate prompt
improvements that temporarily increase spending. These metrics should trend, not gate.

### "Compare against bump-pydantic or other tools"
For future phases (Spring Boot 2→3, JUnit4→5). J2K is the right benchmark for v1.

### "Run evals on every commit"
Too expensive. PRs get the fast subset; full corpus runs nightly. This is the
right CI/cost balance at this scale.

---

## Consequences

- `evals/runner.py` becomes the canonical eval entrypoint
- `evals/baselines/java_to_kotlin.json` must be kept current (nightly CI updates it)
- Every change touching `src/migration/` or `src/agent_core/models.py` triggers evals
- PR acceptance rate requires Langfuse integration (Phase 4 trace hook) to track automatically
