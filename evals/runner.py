"""Eval runner — produces the scorecard per repo and across the corpus.

Usage:
    # Run one repo and print scorecard
    python -m evals.runner --repo https://github.com/org/repo --profile java_to_kotlin

    # Run full corpus and save
    python -m evals.runner --corpus evals/corpus/manifest.toml --profile java_to_kotlin \
        --output evals/results/run_2026-05-31.json

    # CI mode: fail if regression vs baseline
    python -m evals.runner --corpus evals/corpus/manifest.toml --profile java_to_kotlin \
        --baseline evals/baselines/java_to_kotlin.json --ci

    # Save current run as new baseline
    python -m evals.runner --corpus evals/corpus/manifest.toml --profile java_to_kotlin \
        --save-baseline evals/baselines/java_to_kotlin.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import statistics
import sys
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── dataclasses ───────────────────────────────────────────────────────────────

@dataclasses.dataclass
class FileScore:
    path: str
    success: bool
    gave_up: bool
    fix_attempts: int
    cost_usd: float
    diff_size_lines: int
    rules_applied: list[str]
    critic_verdict: str


@dataclasses.dataclass
class RepoScore:
    repo_url: str
    profile_name: str
    file_scores: list[FileScore]
    error: str = ""          # non-empty if the entire run failed (clone error, etc.)

    @property
    def success_rate(self) -> float:
        if not self.file_scores:
            return 0.0
        return sum(1 for f in self.file_scores if f.success) / len(self.file_scores)

    @property
    def gave_up_rate(self) -> float:
        if not self.file_scores:
            return 0.0
        return sum(1 for f in self.file_scores if f.gave_up) / len(self.file_scores)

    @property
    def avg_fix_attempts(self) -> float:
        if not self.file_scores:
            return 0.0
        return statistics.mean(f.fix_attempts for f in self.file_scores)

    @property
    def total_cost_usd(self) -> float:
        return sum(f.cost_usd for f in self.file_scores)

    @property
    def total_diff_lines(self) -> int:
        return sum(f.diff_size_lines for f in self.file_scores)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_url": self.repo_url,
            "profile_name": self.profile_name,
            "error": self.error,
            "files_total": len(self.file_scores),
            "files_succeeded": sum(1 for f in self.file_scores if f.success),
            "files_gave_up": sum(1 for f in self.file_scores if f.gave_up),
            "success_rate": round(self.success_rate, 4),
            "gave_up_rate": round(self.gave_up_rate, 4),
            "avg_fix_attempts": round(self.avg_fix_attempts, 2),
            "total_cost_usd": round(self.total_cost_usd, 4),
            "total_diff_lines": self.total_diff_lines,
            "files": [dataclasses.asdict(f) for f in self.file_scores],
        }


@dataclasses.dataclass
class Scorecard:
    run_id: str
    profile_name: str
    repo_scores: list[RepoScore]

    @property
    def all_files(self) -> list[FileScore]:
        return [f for r in self.repo_scores for f in r.file_scores]

    @property
    def overall_success_rate(self) -> float:
        files = self.all_files
        return sum(1 for f in files if f.success) / len(files) if files else 0.0

    @property
    def overall_gave_up_rate(self) -> float:
        files = self.all_files
        return sum(1 for f in files if f.gave_up) / len(files) if files else 0.0

    @property
    def total_cost_usd(self) -> float:
        return sum(r.total_cost_usd for r in self.repo_scores)

    @property
    def avg_fix_attempts(self) -> float:
        files = self.all_files
        return statistics.mean(f.fix_attempts for f in files) if files else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "profile_name": self.profile_name,
            "repos_total": len(self.repo_scores),
            "files_total": len(self.all_files),
            "overall_success_rate": round(self.overall_success_rate, 4),
            "overall_gave_up_rate": round(self.overall_gave_up_rate, 4),
            "avg_fix_attempts": round(self.avg_fix_attempts, 2),
            "total_cost_usd": round(self.total_cost_usd, 4),
            "repos": [r.to_dict() for r in self.repo_scores],
        }

    def to_markdown_table(self) -> str:
        lines = [
            "## Migration Scorecard",
            "",
            f"**Profile:** `{self.profile_name}`  |  "
            f"**Overall success:** {self.overall_success_rate:.1%}  |  "
            f"**Total cost:** ${self.total_cost_usd:.4f}",
            "",
            "| Repo | Files | ✅ Pass | ⛔ Gave Up | Avg Retries | Cost | Diff Lines |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in self.repo_scores:
            name = r.repo_url.split("/")[-1]
            if r.error:
                lines.append(f"| {name} | — | — | — | — | — | **ERROR**: {r.error[:60]} |")
            else:
                ok = sum(1 for f in r.file_scores if f.success)
                gave = sum(1 for f in r.file_scores if f.gave_up)
                lines.append(
                    f"| {name} | {len(r.file_scores)} | {ok} ({r.success_rate:.0%}) "
                    f"| {gave} ({r.gave_up_rate:.0%}) "
                    f"| {r.avg_fix_attempts:.1f} "
                    f"| ${r.total_cost_usd:.3f} "
                    f"| {r.total_diff_lines} |"
                )
        n = len(self.all_files)
        lines.append(
            f"| **TOTAL** | **{n}** | **{self.overall_success_rate:.1%}** "
            f"| **{self.overall_gave_up_rate:.1%}** "
            f"| **{self.avg_fix_attempts:.1f}** "
            f"| **${self.total_cost_usd:.3f}** | — |"
        )
        return "\n".join(lines)


# ── regression gate ───────────────────────────────────────────────────────────

def check_regression(current: Scorecard, baseline_path: Path) -> tuple[bool, str]:
    """Return (regressed, message). Fails if success_rate drops > 1pp vs baseline."""
    if not baseline_path.exists():
        return False, "No baseline found — pass by default."

    with open(baseline_path) as f:
        baseline = json.load(f)

    prev_rate = baseline.get("overall_success_rate", 0.0)
    curr_rate = current.overall_success_rate
    delta = curr_rate - prev_rate

    if delta < -0.01:
        return True, (
            f"REGRESSION: success_rate dropped from {prev_rate:.1%} to {curr_rate:.1%} "
            f"(Δ {delta:+.1%})"
        )
    return False, (
        f"OK: success_rate {curr_rate:.1%} vs baseline {prev_rate:.1%} (Δ {delta:+.1%})"
    )


# ── state → scores extractor ──────────────────────────────────────────────────

def extract_repo_score(final_state: dict, repo_url: str, profile_name: str) -> RepoScore:
    """Convert a completed LangGraph final state into a RepoScore."""
    records = final_state.get("file_eval_records", [])
    file_scores = [
        FileScore(
            path=r["path"],
            success=r["success"],
            gave_up=r.get("gave_up", False),
            fix_attempts=r["fix_attempts"],
            cost_usd=r["cost_usd"],
            diff_size_lines=r["diff_size_lines"],
            rules_applied=r.get("rules_applied", []),
            critic_verdict=r.get("critic_verdict", ""),
        )
        for r in records
    ]
    return RepoScore(repo_url=repo_url, profile_name=profile_name, file_scores=file_scores)


# ── single-repo runner ────────────────────────────────────────────────────────

def run_eval_on_repo(repo_url: str, profile_name: str) -> RepoScore:
    """Run the migration agent on one repo with HITL_LEVEL=none and collect scores."""
    os.environ["HITL_LEVEL"] = "none"
    os.environ.setdefault("SANDBOX_BACKEND", "local")

    from migration.graph import build_graph

    graph = build_graph()
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    initial_state: dict = {
        "repo_url": repo_url,
        "profile_name": profile_name,
        "run_id": thread_id,
        "errors": [],
    }

    log.info("eval: %s  profile=%s  thread=%s", repo_url, profile_name, thread_id)

    try:
        final_state: dict = {}
        for event in graph.stream(initial_state, config, stream_mode="values"):
            final_state = event

        snapshot = graph.get_state(config)
        if snapshot.values:
            final_state = snapshot.values

        return extract_repo_score(final_state, repo_url, profile_name)
    except Exception as e:
        log.error("eval failed for %s: %s", repo_url, e)
        return RepoScore(
            repo_url=repo_url,
            profile_name=profile_name,
            file_scores=[],
            error=str(e),
        )


# ── corpus runner ─────────────────────────────────────────────────────────────

def load_corpus(manifest_path: Path) -> list[str]:
    """Load repo URLs from a TOML manifest.

    If EVAL_CI_FAST_ONLY=true, only returns repos with ci_fast=true.
    If a repo has ci_skip=true, it is always excluded.
    """
    import tomllib
    with open(manifest_path, "rb") as f:
        data = tomllib.load(f)
    fast_only = os.environ.get("EVAL_CI_FAST_ONLY", "").lower() == "true"
    return [
        r["url"]
        for r in data.get("repos", [])
        if not r.get("ci_skip", False)
        and (not fast_only or r.get("ci_fast", False))
    ]


def run_corpus(manifest_path: Path, profile_name: str) -> Scorecard:
    run_id = str(uuid.uuid4())
    repo_urls = load_corpus(manifest_path)
    log.info("Running eval corpus: %d repos  run_id=%s", len(repo_urls), run_id)

    repo_scores: list[RepoScore] = []
    for url in repo_urls:
        score = run_eval_on_repo(url, profile_name)
        repo_scores.append(score)
        log.info(
            "  %s: %d files  %.1f%% pass  $%.4f",
            url.split("/")[-1],
            len(score.file_scores),
            score.success_rate * 100,
            score.total_cost_usd,
        )

    return Scorecard(run_id=run_id, profile_name=profile_name, repo_scores=repo_scores)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(description="code-migration-agent eval runner")
    parser.add_argument("--repo", help="Single repo URL to evaluate")
    parser.add_argument("--corpus", type=Path, help="Path to corpus manifest.toml")
    parser.add_argument("--profile", default="java_to_kotlin", help="Migration profile")
    parser.add_argument("--output", type=Path, help="Save scorecard JSON to this path")
    parser.add_argument("--baseline", type=Path, help="Baseline JSON to compare against")
    parser.add_argument("--save-baseline", type=Path, dest="save_baseline",
                        help="Save current scorecard as baseline")
    parser.add_argument("--ci", action="store_true",
                        help="Exit 1 if regression vs baseline (CI mode)")
    parser.add_argument("--post-comment", metavar="PR_NUMBER",
                        help="Post scorecard as comment on this GitHub PR number")
    args = parser.parse_args()

    # Build scorecard
    if args.repo:
        repo_score = run_eval_on_repo(args.repo, args.profile)
        scorecard = Scorecard(
            run_id=str(uuid.uuid4()),
            profile_name=args.profile,
            repo_scores=[repo_score],
        )
    elif args.corpus:
        scorecard = run_corpus(args.corpus, args.profile)
    else:
        parser.print_help()
        sys.exit(1)

    # Print markdown table
    print("\n" + scorecard.to_markdown_table() + "\n")

    # Save output
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(scorecard.to_dict(), f, indent=2)
        log.info("Scorecard saved → %s", args.output)

    # Save as baseline
    if args.save_baseline:
        args.save_baseline.parent.mkdir(parents=True, exist_ok=True)
        with open(args.save_baseline, "w") as f:
            json.dump(scorecard.to_dict(), f, indent=2)
        log.info("Baseline saved → %s", args.save_baseline)

    # Post PR comment
    if args.post_comment:
        _post_pr_comment(scorecard, args.post_comment)

    # CI regression gate
    if args.ci:
        baseline_path = args.baseline or Path("evals/baselines") / f"{args.profile}.json"
        regressed, msg = check_regression(scorecard, baseline_path)
        print(f"\nCI gate: {msg}")
        if regressed:
            sys.exit(1)


def _post_pr_comment(scorecard: Scorecard, pr_number: str) -> None:
    """Post the scorecard markdown as a GitHub PR comment via gh CLI."""
    import subprocess
    body = scorecard.to_markdown_table()
    result = subprocess.run(
        ["gh", "pr", "comment", pr_number, "--body", body],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning("Failed to post PR comment: %s", result.stderr)
    else:
        log.info("Scorecard posted as comment on PR #%s", pr_number)


if __name__ == "__main__":
    main()
