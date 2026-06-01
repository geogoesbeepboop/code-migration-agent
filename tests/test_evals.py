"""Tests for the eval runner — scorecard dataclasses, regression gate, extractor."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from evals.runner import (
    FileScore,
    RepoScore,
    Scorecard,
    check_regression,
    extract_repo_score,
    load_corpus,
)


# ── FileScore / RepoScore ─────────────────────────────────────────────────────

def _make_file(success=True, gave_up=False, fix_attempts=0, cost=0.01,
               diff=20, rules=None, verdict="approve"):
    return FileScore(
        path="Foo.java",
        success=success,
        gave_up=gave_up,
        fix_attempts=fix_attempts,
        cost_usd=cost,
        diff_size_lines=diff,
        rules_applied=rules or ["R01"],
        critic_verdict=verdict,
    )


class TestRepoScore:
    def test_success_rate_all_pass(self):
        r = RepoScore("url", "java_to_kotlin", [_make_file(True), _make_file(True)])
        assert r.success_rate == 1.0

    def test_success_rate_mixed(self):
        r = RepoScore("url", "j2k", [_make_file(True), _make_file(False)])
        assert r.success_rate == 0.5

    def test_success_rate_empty(self):
        assert RepoScore("url", "j2k", []).success_rate == 0.0

    def test_gave_up_rate(self):
        r = RepoScore("url", "j2k", [_make_file(False, gave_up=True), _make_file(True)])
        assert r.gave_up_rate == 0.5

    def test_avg_fix_attempts(self):
        r = RepoScore("url", "j2k", [_make_file(fix_attempts=2), _make_file(fix_attempts=0)])
        assert r.avg_fix_attempts == pytest.approx(1.0)

    def test_total_cost(self):
        r = RepoScore("url", "j2k", [_make_file(cost=0.01), _make_file(cost=0.02)])
        assert r.total_cost_usd == pytest.approx(0.03)

    def test_to_dict_keys(self):
        r = RepoScore("url", "j2k", [_make_file()])
        d = r.to_dict()
        assert "success_rate" in d
        assert "files_total" in d
        assert "files" in d

    def test_error_field(self):
        r = RepoScore("url", "j2k", [], error="clone failed")
        assert r.error == "clone failed"


class TestScorecard:
    def _card(self, rates):
        repos = []
        for rate in rates:
            n = 10
            ok = int(n * rate)
            files = [_make_file(True)] * ok + [_make_file(False)] * (n - ok)
            repos.append(RepoScore("url", "j2k", files))
        return Scorecard("run1", "j2k", repos)

    def test_overall_success_rate(self):
        card = self._card([1.0, 0.5])
        assert card.overall_success_rate == pytest.approx(0.75)

    def test_total_cost(self):
        card = self._card([1.0])
        assert card.total_cost_usd == pytest.approx(10 * 0.01)

    def test_to_dict_structure(self):
        card = self._card([1.0])
        d = card.to_dict()
        assert "overall_success_rate" in d
        assert "repos" in d
        assert d["files_total"] == 10

    def test_markdown_table_contains_headers(self):
        card = self._card([1.0])
        table = card.to_markdown_table()
        assert "Migration Scorecard" in table
        assert "Pass" in table
        assert "TOTAL" in table

    def test_markdown_table_error_repo(self):
        r = RepoScore("url", "j2k", [], error="clone failed")
        card = Scorecard("run1", "j2k", [r])
        table = card.to_markdown_table()
        assert "ERROR" in table


# ── check_regression ──────────────────────────────────────────────────────────

class TestCheckRegression:
    def test_no_baseline_passes(self, tmp_path):
        card = Scorecard("r", "j2k", [])
        regressed, msg = check_regression(card, tmp_path / "no_file.json")
        assert not regressed

    def test_improvement_passes(self, tmp_path):
        baseline = {"overall_success_rate": 0.80}
        (tmp_path / "baseline.json").write_text(json.dumps(baseline))
        card = Scorecard("r", "j2k", [RepoScore("u", "j2k", [_make_file(True)] * 9 + [_make_file(False)])])
        regressed, _ = check_regression(card, tmp_path / "baseline.json")
        assert not regressed  # 0.90 vs 0.80 = improvement

    def test_small_regression_within_tolerance_passes(self, tmp_path):
        baseline = {"overall_success_rate": 0.80}
        (tmp_path / "baseline.json").write_text(json.dumps(baseline))
        # 0.795 is 0.005pp below baseline — within 1pp tolerance
        files = [_make_file(True)] * 795 + [_make_file(False)] * 205
        card = Scorecard("r", "j2k", [RepoScore("u", "j2k", files)])
        regressed, _ = check_regression(card, tmp_path / "baseline.json")
        assert not regressed

    def test_large_regression_fails(self, tmp_path):
        baseline = {"overall_success_rate": 0.80}
        (tmp_path / "baseline.json").write_text(json.dumps(baseline))
        # 0.60 is 20pp below baseline
        files = [_make_file(True)] * 6 + [_make_file(False)] * 4
        card = Scorecard("r", "j2k", [RepoScore("u", "j2k", files)])
        regressed, msg = check_regression(card, tmp_path / "baseline.json")
        assert regressed
        assert "REGRESSION" in msg

    def test_exact_at_baseline_passes(self, tmp_path):
        baseline = {"overall_success_rate": 0.80}
        (tmp_path / "baseline.json").write_text(json.dumps(baseline))
        files = [_make_file(True)] * 8 + [_make_file(False)] * 2
        card = Scorecard("r", "j2k", [RepoScore("u", "j2k", files)])
        regressed, _ = check_regression(card, tmp_path / "baseline.json")
        assert not regressed


# ── extract_repo_score ────────────────────────────────────────────────────────

class TestExtractRepoScore:
    def test_extracts_file_records(self):
        state = {
            "file_eval_records": [
                {"path": "A.java", "success": True, "gave_up": False,
                 "fix_attempts": 0, "cost_usd": 0.01, "diff_size_lines": 10,
                 "rules_applied": ["R01"], "critic_verdict": "approve"},
                {"path": "B.java", "success": False, "gave_up": True,
                 "fix_attempts": 3, "cost_usd": 0.05, "diff_size_lines": 0,
                 "rules_applied": [], "critic_verdict": ""},
            ]
        }
        score = extract_repo_score(state, "https://github.com/org/repo", "j2k")
        assert len(score.file_scores) == 2
        assert score.file_scores[0].success
        assert score.file_scores[1].gave_up
        assert score.success_rate == 0.5

    def test_empty_records(self):
        score = extract_repo_score({}, "url", "j2k")
        assert score.file_scores == []
        assert score.success_rate == 0.0


# ── load_corpus ───────────────────────────────────────────────────────────────

class TestLoadCorpus:
    def test_loads_all_repos(self):
        manifest = Path("evals/corpus/manifest.toml")
        if not manifest.exists():
            pytest.skip("manifest not found")
        urls = load_corpus(manifest)
        assert len(urls) > 0
        assert all(u.startswith("http") for u in urls)

    def test_fast_only_filter(self, tmp_path, monkeypatch):
        manifest = tmp_path / "manifest.toml"
        manifest.write_text(textwrap.dedent("""\
            [[repos]]
            url = "https://github.com/fast/repo"
            ci_fast = true

            [[repos]]
            url = "https://github.com/slow/repo"
            ci_fast = false

            [[repos]]
            url = "https://github.com/skip/repo"
            ci_skip = true
        """))
        monkeypatch.setenv("EVAL_CI_FAST_ONLY", "true")
        urls = load_corpus(manifest)
        assert urls == ["https://github.com/fast/repo"]

    def test_skip_excluded(self, tmp_path, monkeypatch):
        manifest = tmp_path / "manifest.toml"
        manifest.write_text(textwrap.dedent("""\
            [[repos]]
            url = "https://github.com/a/b"

            [[repos]]
            url = "https://github.com/c/d"
            ci_skip = true
        """))
        monkeypatch.delenv("EVAL_CI_FAST_ONLY", raising=False)
        urls = load_corpus(manifest)
        assert urls == ["https://github.com/a/b"]


# ── next_file diff counting ───────────────────────────────────────────────────

class TestCountDiffLines:
    def test_counts_added_and_removed(self):
        from migration.nodes.next_file import _count_diff_lines
        patch = textwrap.dedent("""\
            --- a/Foo.java
            +++ b/Foo.java
            @@ -1,4 +1,4 @@
             public class Foo {
            -    private String name = null;
            +    private String? name = null
             }
        """)
        assert _count_diff_lines(patch) == 2  # 1 removed + 1 added

    def test_ignores_headers(self):
        from migration.nodes.next_file import _count_diff_lines
        patch = "--- a/Foo.java\n+++ b/Foo.java\n@@ -1,1 +1,1 @@\n+foo\n"
        assert _count_diff_lines(patch) == 1

    def test_empty_patch(self):
        from migration.nodes.next_file import _count_diff_lines
        assert _count_diff_lines("") == 0


# ── updated graph structure ───────────────────────────────────────────────────

class TestPhase4Graph:
    def test_mark_give_up_node_present(self, monkeypatch):
        monkeypatch.setenv("HITL_LEVEL", "none")
        from migration.graph import build_graph
        graph = build_graph()
        assert "mark_give_up" in graph.nodes

    def test_give_up_sets_flag(self):
        from migration.nodes.verify import mark_give_up
        state = {"current_file_gave_up": False, "some_key": "value"}
        result = mark_give_up(state)
        assert result["current_file_gave_up"] is True
        assert result["some_key"] == "value"  # rest of state preserved
