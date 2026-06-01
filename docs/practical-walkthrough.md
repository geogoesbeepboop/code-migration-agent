# Practical Walkthrough: Java → Kotlin Migration

This document walks through a real end-to-end run of the agent, explains
what to expect at each stage, and itemises every gap that requires action
before the system is production-ready.

---

## Setup checklist

Before running anything, confirm these are in place:

```bash
# 1. API keys
export ANTHROPIC_API_KEY="sk-ant-..."      # required for all LLM calls
export GITHUB_TOKEN="ghp_..."              # required for PR creation only
export E2B_API_KEY="e2b_..."              # only if SANDBOX_BACKEND=e2b

# 2. Python environment
uv sync

# 3. Docker (if SANDBOX_BACKEND=docker)
docker info                                # must respond without error

# 4. Java (if running Gradle tests locally)
java -version                              # must be JDK 17+
```

---

## Example: migrating a small Gradle+Java repo

We'll use a hypothetical `acme/user-service` — a Spring Boot app with 12 Java
files and ~120 JUnit 5 tests. Replace it with any public Gradle+Java repo.

### Step 1 — Start the migration

```bash
python -m migration.cli \
  --repo https://github.com/acme/user-service \
  --profile java_to_kotlin \
  --hitl full
```

**What happens internally:**
1. `ingest`: repo cloned to `/tmp/migration_xxxxx`, tree-sitter parses 12 `.java` files, dep graph built, rule index loaded. For a 12-file repo this takes ~5 seconds.
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

⏸  INTERRUPTED
HITL GATE 1 — Plan approval required.
...
To resume: python -m migration.cli --resume <thread-id>
```

**What to check at gate 1:**
- Does the migration order look right? (simple models first, controllers last)
- Are the triggered rules sensible? (R02=data-class, R01=null-safety, R13=@Autowired)
- Is the estimated cost acceptable?
- Any files missing (glob not matching) or unexpected (test files included)?

If something looks off, do NOT resume — fix the profile rules or source glob first.

### Step 2 — Approve the plan

```bash
python -m migration.cli --resume <thread-id>
```

**What happens:** the graph enters the per-file loop. For each file:
1. `worker` loads file content + triggered rules + dep context → calls Sonnet
2. LLM produces a unified diff
3. `verify` applies the diff with `git apply`, runs `./gradlew test`
4. If tests pass → `critic` reviews the diff → `next_file` commits
5. If tests fail → `fix` (Opus) revises the diff → back to `verify` (up to 3 retries)
6. If retries exhausted → `current_file_gave_up=True` → `critic` raises HITL gate 2

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

### Step 3 — Handle gate 2 (if a file gives up)

If a file exhausts retries, the graph pauses at `critic`:

```
⏸  INTERRUPTED
HITL GATE 2 — Escalation required.

File: src/main/java/com/acme/service/UserService.java
Fix attempts: 3
Last failure:
  FAILED UserServiceTest > testFindById
  error: unresolved reference: findAll

Options: resume to skip this file / edit manually then resume / stop the run.
```

**Your options:**
- **Skip**: resume immediately. The file is reset to HEAD, added to `gave_up_files`.
- **Hand-fix**: edit the file manually in the temp dir, then resume.
- **Stop**: Ctrl-C and fix the rule or prompt before re-running.

For `UserService.java`, the failure is `unresolved reference: findAll` — the
LLM renamed `findAll()` to something else. You'd hand-fix or adjust the rules.

### Step 4 — PR review gate (gate 3)

After all 12 files are processed:

```
⏸  INTERRUPTED
HITL GATE 3 — PR review.

Profile: java_to_kotlin
Files ready: 11
Files skipped: 1

Changed:
- src/main/java/com/acme/model/User.java
- src/main/java/com/acme/model/Address.java
...

Skipped:
- src/main/java/com/acme/service/UserService.java

Resume to open the PR.
```

**What to check:**
- Browse the temp dir (`state.repo_path`) and `git log --oneline` to see all 11 commits
- `git diff HEAD~11` to review the aggregate diff
- Confirm the skipped file list and decide if you want to hand-fix before opening

```bash
# To inspect
cat .agent-core/checkpoints.db  # checkpoint persists state between sessions
cd /tmp/migration_xxxxx && git log --oneline
```

### Step 5 — Open the PR

```bash
python -m migration.cli --resume <thread-id>
```

**Expected output:**
```
INFO  PR opened: https://github.com/acme/user-service/pull/42
✅ MIGRATION COMPLETE
   PR:      https://github.com/acme/user-service/pull/42
   Files:   11 migrated, 1 skipped
   Cost:    $0.0287 USD
```

The PR has:
- One commit per migrated file with message `chore: migrate <path> [java_to_kotlin]`
- A PR body listing all changed files, skipped files, and cost

---

## Expected outcomes by file complexity

| File type | Expected outcome | Notes |
|---|---|---|
| Simple POJO (`@Data` / getters+setters) | ✅ Pass first try | R02 data class conversion is reliable |
| Repository interface (Spring Data) | ✅ Pass first try | Mostly removing `@Autowired` (R13) |
| Service with null checks | ✅ Pass, 0–1 retries | R01/R12 null-safety needs precise handling |
| Controller with switch statements | ✅ Pass, 1–2 retries | R05 switch→when can need iteration |
| Complex service with async | ⚠️ Likely gave_up | R08 (coroutines) is flagged as high-risk |
| Utility class with statics | ✅ Pass, 0–1 retries | R06/R07 extension functions + companion |

---

## Running without HITL (eval mode)

For benchmarking / corpus evaluation:

```bash
HITL_LEVEL=none python -m migration.cli \
  --repo https://github.com/acme/user-service \
  --profile java_to_kotlin

# Or via the eval runner:
python -m evals.runner \
  --repo https://github.com/acme/user-service \
  --profile java_to_kotlin
```

This runs fully autonomously (no interruptions) and prints the scorecard.

---

## Gaps — what you need to address

### 🔴 Blocking (the agent will not work without these)

**1. `ANTHROPIC_API_KEY` not set**
The worker, fix, and critic nodes all call Anthropic. Without this:
```
KeyError: 'ANTHROPIC_API_KEY'
```
→ Set it: `export ANTHROPIC_API_KEY=sk-ant-...`

**2. No Gradle-compatible repo**
The test command is hardcoded to `./gradlew test`. If your repo uses Maven:
- Create a `profiles/maven_java_to_kotlin/tests.toml` with `command = "./mvnw test"`
- The graph is profile-agnostic; only `tests.toml` changes

**3. Git user not configured (for commits)**
Fixed in Phase 4a — `ingest` sets `user.email` and `user.name` locally.
If that still fails, run: `git config --global user.email "your@email.com"`

**4. `GITHUB_TOKEN` not set (for PR creation)**
Without this the `pr` node logs a warning and sets `pr_url = "(no GITHUB_TOKEN)"`.
The migration still runs; you just won't get an auto-opened PR.
→ Set it: `export GITHUB_TOKEN=ghp_...`

### 🟡 Important (degraded experience without these)

**5. Docker not running (for `SANDBOX_BACKEND=docker`)**
The agent falls back to `LocalSandbox` automatically, which runs test commands
directly on your host. This means:
- Test commands run on your machine (not isolated)
- You need the correct JDK version installed locally
→ Either start Docker Desktop or leave `SANDBOX_BACKEND=local` (the default)

**6. IntelliJ J2K baseline not measured**
`evals/baselines/java_to_kotlin.json` is a stub with `overall_success_rate: 0.0`.
This means the CI gate trivially passes (anything beats 0%).
→ **Required action**: run IntelliJ J2K manually on the corpus repos, record
pass/fail per file, and update the baseline:
```bash
python -m evals.runner \
  --corpus evals/corpus/manifest.toml \
  --profile java_to_kotlin \
  --save-baseline evals/baselines/java_to_kotlin.json
```
(first run with the agent establishes the agent baseline; J2K baseline requires
the manual IntelliJ process described in ADR 001)

**7. Corpus repos not verified**
The `manifest.toml` lists 8 repos but none have been verified to have passing
Gradle test suites. Before running evals:
```bash
for repo in iluwatar/java-design-patterns TheAlgorithms/Java google/gson; do
  git clone https://github.com/$repo /tmp/eval_$repo
  cd /tmp/eval_$repo && ./gradlew test --no-daemon
done
```
Remove any repo where `./gradlew test` fails before adding to the corpus.

**8. GitHub Actions secrets not configured**
The CI workflow requires `ANTHROPIC_API_KEY` as a repository secret.
→ GitHub repo → Settings → Secrets → Actions → New repository secret

### 🟢 Nice to have (Phase 5)

**9. Langfuse tracing not wired in**
`langfuse_trace_url` is always `""`. The PR body references it as a placeholder.
→ Phase 4 ADR notes this; add Langfuse in Phase 4 refinement or Phase 5.

**10. `PR acceptance rate` not auto-tracked**
The north-star metric requires humans to actually merge (or reject) generated PRs.
Currently tracked manually. Langfuse + a GitHub webhook would automate this.

**11. E2B sandbox not tested end-to-end**
The `E2BSandbox` implementation is complete and matches the v2 API, but hasn't
been run against a live E2B account. Upload latency for large repos is unknown.
→ Test with `E2B_API_KEY` set: `SANDBOX_BACKEND=e2b python -m migration.cli --repo ...`

**12. Maven profile missing**
Most of the target repos in the corpus use Maven, not Gradle. The agent
architecture is profile-agnostic; adding Maven support only requires:
- `src/migration/profiles/maven_java_to_kotlin/tests.toml`  (command = `./mvnw test`)
- `src/migration/profiles/maven_java_to_kotlin/rules.md`    (same rules)

---

## Gaps — what I need to address (code side)

These are known implementation gaps identified during the walkthrough:

**A. Worker's NodeInterrupt at `current_file_index == 0` is now gone (fixed)**
The HITL gate 1 is correctly handled via `interrupt_before=["plan_review"]`. ✅

**B. Critic `give_up` path sets `current_file_gave_up = True` — missing**
When `route_after_verify` returns `"give_up"`, the graph goes to `critic` but
`current_file_gave_up` in state is still `False`. The critic then doesn't know
it's a give-up escalation case.

→ **Fix needed in `verify.py`**: set `current_file_gave_up = True` in state
when returning `give_up` routing. Currently `verify` returns the `test_result`
but doesn't set `current_file_gave_up` — that flag is never set to `True`.

**C. `plan_review` interrupt resumes with `current_file_index = 0`**
On resume, `worker` is called with `idx=0`. This is correct — worker migrates
`migration_order[0]`. But the test suite should cover this path explicitly.

**D. `pr.py` branch creation happens after commits are on `HEAD~main`**
The `git checkout -b migration/...` in `pr.py` creates a branch at the current
HEAD, which has all migration commits. Pushing this branch is correct. ✅
But if the repo has `main` branch protection (requires PR), the local `main`
commits never get pushed to remote's `main`, which is the correct behavior. ✅

**E. Corpus manifest repos need Gradle support verification**
`TheAlgorithms/Java` uses Gradle but the test command may need adjustment
(some modules don't have `gradlew` at the root). The manifest notes this.
