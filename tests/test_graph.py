"""Tests for the LangGraph graph structure and routing logic."""

import os

import pytest

from migration.graph import build_graph
from migration.hitl import HITLLevel, hitl_gates
from migration.nodes.verify import route_after_verify
from migration.nodes.next_file import route_from_next_file


class TestHITLGates:
    def test_full_gates(self, monkeypatch):
        monkeypatch.setenv("HITL_LEVEL", "full")
        gates = hitl_gates()
        assert "plan_review" in gates
        assert "pr" in gates

    def test_plan_only_gates(self, monkeypatch):
        monkeypatch.setenv("HITL_LEVEL", "plan_only")
        gates = hitl_gates()
        assert "plan_review" in gates
        assert "pr" not in gates

    def test_none_gates(self, monkeypatch):
        monkeypatch.setenv("HITL_LEVEL", "none")
        gates = hitl_gates()
        assert gates == []


class TestRouteAfterVerify:
    def test_pass_on_success(self):
        state = {"test_result": {"success": True, "output": "", "failures": []}}
        assert route_after_verify(state) == "pass"

    def test_fix_on_first_failure(self):
        state = {
            "test_result": {"success": False, "output": "FAILED", "failures": []},
            "fix_attempts": 0,
        }
        assert route_after_verify(state) == "fix"

    def test_give_up_on_max_attempts(self, monkeypatch):
        monkeypatch.setenv("MAX_FIX_ATTEMPTS", "3")
        state = {
            "test_result": {"success": False, "output": "FAILED", "failures": []},
            "fix_attempts": 3,
        }
        assert route_after_verify(state) == "give_up"


class TestRouteFromNextFile:
    def test_routes_to_worker_when_more_files(self):
        state = {
            "current_file_index": 1,  # just advanced
            "migration_order": [{"path": "A.java", "rules": [], "deps": []},
                                 {"path": "B.java", "rules": [], "deps": []}],
        }
        assert route_from_next_file(state) == "worker"

    def test_routes_to_pr_when_done(self):
        state = {
            "current_file_index": 2,  # past end
            "migration_order": [{"path": "A.java", "rules": [], "deps": []},
                                 {"path": "B.java", "rules": [], "deps": []}],
        }
        assert route_from_next_file(state) == "pr"


class TestBuildGraph:
    def test_build_succeeds(self, monkeypatch):
        monkeypatch.setenv("HITL_LEVEL", "none")
        graph = build_graph()
        assert graph is not None

    def test_expected_nodes_present(self, monkeypatch):
        monkeypatch.setenv("HITL_LEVEL", "none")
        graph = build_graph()
        nodes = set(graph.nodes.keys())
        for expected in ["ingest", "plan", "plan_review", "worker", "verify",
                         "fix", "critic", "next_file", "pr"]:
            assert expected in nodes, f"Missing node: {expected}"
