"""Shared LangGraph state. Every node reads from and writes to this TypedDict."""

from __future__ import annotations

from typing import TypedDict


class FileTask(TypedDict):
    path: str           # repo-relative path (e.g. "src/main/java/com/example/Foo.java")
    rules: list[str]    # rule IDs that fire on this file (e.g. ["R01", "R03"])
    deps: list[str]     # paths this file depends on (already migrated before this)


class TestResult(TypedDict):
    success: bool
    output: str         # full test stdout/stderr
    failures: list[str] # parsed failure messages fed back to fix node


class AppliedPatch(TypedDict):
    path: str
    patch: str
    commit_sha: str


class FileEvalRecord(TypedDict):
    """Per-file metrics emitted after each file is processed. Used by evals/runner.py."""
    path: str
    success: bool           # tests passed
    gave_up: bool           # exhausted retries
    fix_attempts: int       # how many fix loops were needed
    cost_usd: float         # LLM cost for worker + all fix + critic calls
    diff_size_lines: int    # changed lines in the final patch
    rules_applied: list[str]
    critic_verdict: str     # "approve" | "revise" | "escalate" | ""


class MigrationState(TypedDict, total=False):
    # ── inputs (set before graph starts) ──────────────────────────────────────
    repo_url: str
    profile_name: str       # e.g. "java_to_kotlin"

    # ── ingest output ─────────────────────────────────────────────────────────
    repo_path: str          # absolute path to local sandbox clone
    run_id: str             # unique ID for this run (used for budget registry)

    # ── plan output ───────────────────────────────────────────────────────────
    dep_graph: dict[str, list[str]]   # serialised for state persistence
    migration_order: list[FileTask]   # topologically sorted
    plan_summary: str                 # human-readable for HITL gate 1
    estimated_cost_usd: float

    # ── per-file loop ──────────────────────────────────────────────────────────
    current_file_index: int           # index into migration_order
    current_file: FileTask            # populated by worker
    patch: str                        # unified diff from worker / fix
    test_result: TestResult
    fix_attempts: int
    current_file_gave_up: bool        # True when retries exhausted → HITL gate 2
    current_file_cost_usd: float      # LLM cost for this file's worker+fix+critic

    # ── accumulated results ────────────────────────────────────────────────────
    applied_patches: list[AppliedPatch]   # successfully migrated files
    gave_up_files: list[str]              # files skipped due to exhausted retries
    file_eval_records: list[FileEvalRecord]  # one per file, for scorecard

    # ── critic ────────────────────────────────────────────────────────────────
    critic_verdict: str     # "approve" | "revise" | "escalate"
    critic_notes: str

    # ── PR output ─────────────────────────────────────────────────────────────
    pr_url: str
    langfuse_trace_url: str

    # ── sandbox (Phase 3) ─────────────────────────────────────────────────────
    sandbox_container_id: str   # Docker container ID — set by ingest, used by verify
    e2b_sandbox_id: str         # E2B sandbox ID — set by ingest for CI backend

    # ── bookkeeping ───────────────────────────────────────────────────────────
    errors: list[str]
    total_cost_usd: float
    human_interventions: int
