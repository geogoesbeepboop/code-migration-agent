"""Tests for dependency graph: parsing, edge building, topological sort."""

import os
import textwrap
from pathlib import Path

import pytest

from migration.depgraph import (
    DependencyGraph,
    _extract_java_metadata,
    build_dependency_graph,
)


# ── unit tests for DependencyGraph ────────────────────────────────────────────

class TestDependencyGraph:
    def test_topo_order_leaf_first(self):
        g = DependencyGraph()
        g.add_edge("C.java", "B.java")  # C depends on B
        g.add_edge("B.java", "A.java")  # B depends on A
        order = g.topological_order()
        assert order == ["A.java", "B.java", "C.java"]

    def test_topo_order_diamond(self):
        g = DependencyGraph()
        g.add_edge("D.java", "B.java")
        g.add_edge("D.java", "C.java")
        g.add_edge("B.java", "A.java")
        g.add_edge("C.java", "A.java")
        order = g.topological_order()
        assert order.index("A.java") == 0
        assert order.index("D.java") == len(order) - 1

    def test_cycle_raises(self):
        g = DependencyGraph()
        g.add_edge("A.java", "B.java")
        g.add_edge("B.java", "A.java")
        with pytest.raises(ValueError, match="Cycle"):
            g.topological_order()

    def test_isolated_nodes(self):
        g = DependencyGraph()
        g.add_file("A.java")
        g.add_file("B.java")
        order = g.topological_order()
        assert set(order) == {"A.java", "B.java"}

    def test_to_dict(self):
        g = DependencyGraph()
        g.add_edge("B.java", "A.java")
        d = g.to_dict()
        assert d == {"B.java": ["A.java"], "A.java": []}


# ── unit tests for Java AST extraction ────────────────────────────────────────

class TestExtractJavaMetadata:
    def test_package_and_class(self):
        src = textwrap.dedent("""\
            package com.example.service;
            public class UserService {}
        """).encode()
        package, cls, imports = _extract_java_metadata(src)
        assert package == "com.example.service"
        assert cls == "UserService"
        assert imports == []

    def test_imports_extracted(self):
        src = textwrap.dedent("""\
            package com.example;
            import com.example.model.User;
            import java.util.List;
            public class UserService {}
        """).encode()
        _, _, imports = _extract_java_metadata(src)
        assert "com.example.model.User" in imports
        assert "java.util.List" in imports

    def test_static_import_excluded(self):
        src = textwrap.dedent("""\
            package com.example;
            import static com.example.utils.Helpers.doSomething;
            public class Foo {}
        """).encode()
        _, _, imports = _extract_java_metadata(src)
        assert imports == []

    def test_interface_detected(self):
        src = textwrap.dedent("""\
            package com.example;
            public interface UserRepository {}
        """).encode()
        _, cls, _ = _extract_java_metadata(src)
        assert cls == "UserRepository"


# ── integration test: build dep graph from temp repo ─────────────────────────

class TestBuildDependencyGraph:
    def _make_java_repo(self, tmp_path: Path) -> Path:
        """Create a minimal Java project structure."""
        src = tmp_path / "src/main/java/com/example"
        src.mkdir(parents=True)

        (src / "User.java").write_text(textwrap.dedent("""\
            package com.example;
            public class User { private String name; }
        """))

        (src / "UserRepository.java").write_text(textwrap.dedent("""\
            package com.example;
            import com.example.User;
            public interface UserRepository {
                User findById(Long id);
            }
        """))

        (src / "UserService.java").write_text(textwrap.dedent("""\
            package com.example;
            import com.example.User;
            import com.example.UserRepository;
            public class UserService {
                private UserRepository repo;
            }
        """))

        # Init a git repo so git commands work in verify tests
        os.system(f"cd {tmp_path} && git init -q && git config user.email 't@t.com' && git config user.name 'T'")
        os.system(f"cd {tmp_path} && git add -A && git commit -qm 'init'")
        return tmp_path

    def test_dep_graph_built(self, tmp_path):
        repo = self._make_java_repo(tmp_path)
        graph = build_dependency_graph(repo)
        files = graph.files()
        # All three files should be in the graph
        assert any("User.java" in f for f in files)
        assert any("UserRepository.java" in f for f in files)
        assert any("UserService.java" in f for f in files)

    def test_dep_order_user_first(self, tmp_path):
        repo = self._make_java_repo(tmp_path)
        graph = build_dependency_graph(repo)
        order = graph.topological_order()
        user_idx = next(i for i, f in enumerate(order) if "User.java" in f)
        service_idx = next(i for i, f in enumerate(order) if "UserService.java" in f)
        assert user_idx < service_idx, "User.java must come before UserService.java"
