# Practical Walkthrough: Java → Kotlin Migration

This document walks through a real end-to-end run of the agent, explains
what to expect at each stage, and itemises the remaining gaps.

---

## Setup checklist

Before running anything, confirm these are in place:

```bash
# 1. Required API keys
export ANTHROPIC_API_KEY="sk-ant-..."      # required for all LLM calls
export GITHUB_TOKEN="ghp_..."              # required for PR creation only

# 2. Optional API keys
export E2B_API_KEY="e2b_..."              # only if SANDBOX_BACKEND=e2b
export LANGFUSE_PUBLIC_KEY="pk-lf-..."    # Phase 5 tracing (graceful no-op if absent)
export LANGFUSE_SECRET_KEY="sk-lf-..."

# 3. Python environment
uv sync

# 4. Docker (if SANDBOX_BACKEND=docker)
docker info                                # must respond without error

# 5. Java (if running Gradle tests locally)
java -version                              # must be JDK 17+
```

---

## CLI commands

```bash
# Start a new migration (interactive — stays alive through all gates)
python -m migration.cli migrate \
  --repo https://github.com/acme/user-service \
  --profile java_to_kotlin \
  --hitl full

# Maven repo: use the Maven profile variant
python -m migration.cli migrate \
  --repo https://github.com/acme/user-service \
  --profile java_to_kotlin_maven

# Spring Boot 2 → 3 migration (Gradle or Maven)
python -m migration.cli migrate \
  --repo https://github.com/acme/spring-app \
  --profile spring_boot_2_to_3

# Scaffold a brand-new profile from migration docs
python -m migration.cli scaffold-profile \
  --name my_migration \
  --from "Framework 1.x" \
  --to "Framework 2.x" \
  --sources https://docs.framework.io/migration.html ./local-notes.txt \
  --test-command "./gradlew test"

# Resume an interrupted session (rarely needed — CLI prompts inline now)
python -m migration.cli resume <thread-id>
```

---

## Example: migrating a small Gradle+Java repo

We'll use a hypothetical `acme/user-service` — a Spring Boot app with 12 Java
files and ~120 JUnit 5 tests. Replace it with any public Gradle+Java repo.

### Step 1 — Start the migration

```bash
python -m migration.cli migrate \
  --repo https://github.com/acme/user-service \
  --profile java_to_kotlin \
  --hitl full
```

**What happens internally:**
1. `ingest`: repo cloned to `/tmp/migration_xxxxx`, tree-sitter parses all `.java`
   files, dependency graph built (Kahn's topological sort), rule index loaded from
   `profiles/java_to_kotlin/keywords.toml`. Langfuse trace initialised if keys are set.
2. `plan`: topological sort produces migration order, rules fired per file, cost estimated.
3. Graph pauses at `plan_review` **(HITL gate 1)**.

**Expected output:**
```
=== Migration Plan ===
Profile:         java_to_kotlin
Files to migrate: 12
Estimated cost:  $0.0261 USD

Migration order (dependency-first):
    1. src/main/java/com/acme/model/User.java          [R01, R02]
    2. src/main/java/com/acme/model/Address.java        [R01, R02]
    3. src/main/java/com/acme/repository/UserRepo.java  [R13]
    4. src/main/java/com/acme/service/UserService.java  [R01, R12, R13]
    ...
   12. src/main/java/com/acme/controller/UserCtrl.java  [R05, R12, R13]

============================================================
⏸  INTERRUPTED — gate1
============================================================

[Plan] Approve? [y/n]:
```

The CLI **stays alive** — you interact directly at the prompt.

**What to check at gate 1:**
- Does the migration order look right? (simple models first, controllers last)
- Are the triggered rules sensible? (R02=data-class, R01=null-safety, R13=@Autowired)
- Is the estimated cost acceptable?

**If something looks off**, type `n` and provide feedback:
```
[Plan] Approve? [y/n]: n
Enter feedback / instructions for the agent: Please prioritise files in the
  com.acme.service package and skip test files.
```
The agent incorporates your note and regenerates the plan. You approve on the next round.

### Step 2 — Per-file migration loop (no action needed)

After typing `y` at gate 1, the agent runs through all files automatically. For each:
1. `worker` loads file content + triggered rules + dep context → calls Sonnet
2. LLM produces a unified diff
3. `verify` applies the diff with `git apply`, runs `./gradlew test`
4. If tests pass → `critic` reviews original src + migrated src + diff → `next_file` commits
5. If tests fail → `fix` (Opus) revises the diff → back to `verify` (up to 3 retries)
6. If retries exhausted → failure accumulated silently (deferred gate 2 mode)

**Expected console output per file:**
```
INFO  [1/12] Migrating src/main/java/com/acme/model/User.java
INFO  Running tests in sandbox: ./gradlew test --rerun-tasks --no-daemon
INFO  Tests PASSED (exit=0)
INFO  Critic verdict for .../User.java: approve
INFO  Committed migration of src/main/java/com/acme/model/User.java (a1b2c3d4)
```

**Timing estimate (LocalSandbox, real Gradle):**
- Worker LLM call: ~3–8 seconds (Sonnet)
- Gradle test run: ~30–90 seconds per file (JVM warm-up dominates)
- Full 12-file run: ~10–20 minutes depending on test suite

### Step 3 — Give-up review gate (gate 2, deferred)

After all files are processed, if any gave up:

```
============================================================
⏸  INTERRUPTED — gate2_deferred
HITL GATE 2 (deferred) — The following files exhausted all fix attempts:

  • src/main/java/com/acme/service/UserService.java  (fix attempts: 3)
    Last failure: FAILED UserServiceTest > testFindById
                  error: unresolved reference: findAll
  • src/main/java/com/acme/async/AsyncProcessor.java  (fix attempts: 3)
    Last failure: FAILED AsyncProcessorTest > testProcess
                  error: coroutines not supported

Options:
  [y] Accept all skipped files as-is and continue to PR
  [n] Provide instructions — agent will retry each file with your guidance
============================================================

[Give-up review (all failures)] Approve? [y/n]:
```

**Your options:**
- `y` — accept all skipped files; they will appear in the "Manual review needed"
  section of the PR description.
- `n` + feedback — the agent retries each failed file from scratch with your
  instructions injected into the worker prompt.

> **Note:** To get per-file interruptions instead of the grouped gate, pass
> `--hitl-gate2 immediate` when starting the migration.

### Step 4 — PR review gate (gate 3)

```
============================================================
⏸  INTERRUPTED — gate3

Profile: java_to_kotlin
Files ready: 10  |  Files skipped: 2

Changed:
- src/main/java/com/acme/model/User.java
- src/main/java/com/acme/model/Address.java
...

Skipped:
- src/main/java/com/acme/service/UserService.java
- src/main/java/com/acme/async/AsyncProcessor.java
============================================================

[PR review] Approve? [y/n]:
```

**What to check:**
- Browse the temp dir and `git log --oneline` to see all 10 commits
- `git diff HEAD~10` to review the aggregate diff
- Confirm the skipped file list and decide if you want to hand-fix before opening

**If the PR summary looks wrong**, type `n` and give the agent guidance:
```
[PR review] Approve? [y/n]: n
Enter feedback: The PR description should mention that we deferred all
  async/coroutine files and that the team needs to handle R08 manually.
```
The agent rewrites the PR description using your feedback, then prompts you again.

### Step 5 — PR opens automatically

After typing `y` at gate 3, the agent:
1. Calls the LLM to write a narrative PR description (what was migrated, patterns
   applied, things to watch out for, manual review items)
2. Pushes the migration branch and opens the PR via GitHub API

**Expected output:**
```
INFO  PR opened: https://github.com/acme/user-service/pull/42
============================================================
✅ MIGRATION COMPLETE
   PR:      https://github.com/acme/user-service/pull/42
   Files:   10 migrated, 2 skipped
   Cost:    $0.0287 USD
   Trace:   https://cloud.langfuse.com/trace/abc123  (if Langfuse configured)
============================================================
```

The generated PR has:
- One commit per migrated file: `chore: migrate <path> [java_to_kotlin]`
- An LLM-authored PR body with sections: What was migrated · Patterns applied ·
  Things to watch out for · Manual review needed · Testing notes
- Langfuse trace URL embedded in the PR body (if `LANGFUSE_PUBLIC_KEY` is set)

---

## Available profiles

| Profile | What it migrates | Build tool |
|---|---|---|
| `java_to_kotlin` | Java → Kotlin | Gradle |
| `java_to_kotlin_maven` | Java → Kotlin | Maven |
| `spring_boot_2_to_3` | Spring Boot 2.x → 3.x | Gradle |
| `spring_boot_2_to_3_maven` | Spring Boot 2.x → 3.x | Maven |

Profiles support `inherits` — Maven variants share rules with their Gradle counterpart.

---

## Expected outcomes by file complexity

| File type | Expected outcome | Notes |
|---|---|---|
| Simple POJO (`@Data` / getters+setters) | ✅ Pass first try | R02 data class conversion is reliable |
| Repository interface (Spring Data) | ✅ Pass first try | Mostly removing `@Autowired` (R13) |
| Service with null checks | ✅ Pass, 0–1 retries | R01/R12 null-safety needs precise handling |
| Controller with switch statements | ✅ Pass, 1–2 retries | R05 switch→when can need iteration |
| Complex service with async | ⚠️ Likely gave_up | R08 (coroutines) flagged high-risk |
| Utility class with statics | ✅ Pass, 0–1 retries | R06/R07 extension functions + companion |

---

## Running without HITL (eval mode)

```bash
HITL_LEVEL=none python -m migration.cli migrate \
  --repo https://github.com/acme/user-service \
  --profile java_to_kotlin

# Or via the eval runner:
python -m evals.runner \
  --repo https://github.com/acme/user-service \
  --profile java_to_kotlin
```

This runs fully autonomously (no interruptions) and prints the scorecard.

---

## Gaps — what still requires action

### 🔴 Blocking

**1. `ANTHROPIC_API_KEY` not set**
The worker, fix, and critic nodes all call Anthropic. Without this:
```
KeyError: 'ANTHROPIC_API_KEY'
```
→ `export ANTHROPIC_API_KEY=sk-ant-...`

**2. `GITHUB_TOKEN` not set (for PR creation)**
Without this the `pr` node logs a warning and sets `pr_url = "(no GITHUB_TOKEN)"`.
The migration still runs; you just won't get an auto-opened PR.
→ `export GITHUB_TOKEN=ghp_...`

**3. Git user not configured (for commits)**
`ingest` sets `user.email` and `user.name` locally on the cloned repo.
If commits still fail: `git config --global user.email "your@email.com"`

### 🟡 Important (degraded experience without these)

**4. Docker not running (for `SANDBOX_BACKEND=docker`)**
The agent falls back to `LocalSandbox` automatically, which runs test commands
directly on your host. You need the correct JDK version installed locally.
→ Either start Docker Desktop or leave `SANDBOX_BACKEND=local` (the default)

**5. IntelliJ J2K baseline not measured**
`evals/baselines/java_to_kotlin.json` is a stub with `overall_success_rate: 0.0`.
This means the CI gate trivially passes (anything beats 0%).
→ Run IntelliJ J2K manually on the corpus repos and update the baseline file.

**6. Corpus repos not verified**
The `manifest.toml` lists repos but none have been verified to have passing
Gradle test suites. Before running evals:
```bash
for repo in iluwatar/java-design-patterns TheAlgorithms/Java google/gson; do
  git clone https://github.com/$repo /tmp/eval_$repo
  cd /tmp/eval_$repo && ./gradlew test --no-daemon
done
```
Remove any repo where `./gradlew test` fails before adding to the corpus.

**7. GitHub Actions secrets not configured**
The CI workflow requires `ANTHROPIC_API_KEY` as a repository secret.
→ GitHub repo → Settings → Secrets → Actions → New repository secret

### 🟢 Nice to have

**8. `PR acceptance rate` not auto-tracked**
The north-star metric requires humans to actually merge (or reject) generated PRs.
Currently tracked manually. Langfuse + a GitHub webhook would automate this.

**9. E2B sandbox not tested end-to-end**
The `E2BSandbox` implementation is complete but hasn't been run against a live
E2B account. Upload latency for large repos is unknown.
→ Test with `E2B_API_KEY` set: `SANDBOX_BACKEND=e2b python -m migration.cli migrate --repo ...`

**10. JUnit 4 → 5 profile not yet built**
Planned for Phase 6. The scaffold command can generate a starting point:
```bash
python -m migration.cli scaffold-profile \
  --name junit4_to_junit5 \
  --from "JUnit 4" --to "JUnit 5" \
  --sources https://junit.org/junit5/docs/current/user-guide/#migrating-from-junit4
```
