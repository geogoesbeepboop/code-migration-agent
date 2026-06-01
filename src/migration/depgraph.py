"""Dependency graph builder using tree-sitter.

Parses every Java source file, extracts import/class edges, builds a directed
graph of file-level dependencies, and topologically sorts it so leaf modules
(files with no local deps) migrate first.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import FileTask

log = logging.getLogger(__name__)


# ── tree-sitter setup ──────────────────────────────────────────────────────────

def _get_java_parser():
    import tree_sitter_java as tsjava
    from tree_sitter import Language, Parser
    return Parser(Language(tsjava.language()))


# ── file-level AST extraction ─────────────────────────────────────────────────

def _extract_java_metadata(source: bytes) -> tuple[str, str, list[str]]:
    """Return (package, class_name, [import_fqcns]) for a Java source file."""
    parser = _get_java_parser()
    tree = parser.parse(source)
    root = tree.root_node

    package = ""
    class_name = ""
    imports: list[str] = []

    for node in root.children:
        if node.type == "package_declaration":
            for child in node.children:
                if child.type in ("scoped_identifier", "identifier"):
                    package = child.text.decode()
                    break

        elif node.type == "import_declaration":
            # skip static imports: "import static com.example.Foo.METHOD;"
            is_static = any(c.type == "static" for c in node.children)
            if is_static:
                continue
            for child in node.children:
                if child.type in ("scoped_identifier", "identifier"):
                    imports.append(child.text.decode())
                    break

        elif node.type in (
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "annotation_type_declaration",
            "record_declaration",
        ):
            for child in node.children:
                if child.type == "identifier":
                    class_name = child.text.decode()
                    break

    return package, class_name, imports


# ── DependencyGraph ───────────────────────────────────────────────────────────

class DependencyGraph:
    """Directed graph of file-level dependencies.

    _edges[importer_path] = {importee_path, ...}
    Migration order: importee before importer (leaves first).
    """

    def __init__(self) -> None:
        self._edges: dict[str, set[str]] = {}

    def add_file(self, path: str) -> None:
        self._edges.setdefault(path, set())

    def add_edge(self, importer: str, importee: str) -> None:
        self._edges.setdefault(importer, set()).add(importee)
        self._edges.setdefault(importee, set())

    def dependencies_of(self, file: str) -> list[str]:
        return sorted(self._edges.get(file, set()))

    def files(self) -> list[str]:
        return sorted(self._edges.keys())

    def topological_order(self) -> list[str]:
        """Kahn's algorithm. Leaf (no-dependency) nodes migrate first."""
        in_degree = {f: len(deps) for f, deps in self._edges.items()}

        # reverse: dep → [files that import dep]
        reverse: dict[str, list[str]] = {f: [] for f in self._edges}
        for importer, deps in self._edges.items():
            for dep in deps:
                reverse.setdefault(dep, []).append(importer)

        queue = sorted(f for f, d in in_degree.items() if d == 0)
        order: list[str] = []
        while queue:
            node = queue.pop(0)
            order.append(node)
            for importer in sorted(reverse.get(node, [])):
                in_degree[importer] -= 1
                if in_degree[importer] == 0:
                    queue.append(importer)

        if len(order) != len(self._edges):
            raise ValueError("Cycle detected in dependency graph")
        return order

    def to_dict(self) -> dict[str, list[str]]:
        return {k: sorted(v) for k, v in self._edges.items()}


# ── build from a repo ─────────────────────────────────────────────────────────

def build_dependency_graph(repo_path: Path, source_glob: str = "**/*.java") -> DependencyGraph:
    """Parse all source files matching source_glob and build the dependency graph.

    Algorithm:
    1. Parse every file to get its FQCN (package.ClassName) and imports list.
    2. Build a map: FQCN → repo-relative file path.
    3. For each file, map its imports to local file paths (skip external deps).
    4. Add dependency edges.
    """
    java_files = list(repo_path.glob(source_glob))
    if not java_files:
        log.warning("No files matching %r found in %s", source_glob, repo_path)

    # Pass 1: build FQCN → file path map
    fqcn_to_path: dict[str, str] = {}
    file_imports: dict[str, list[str]] = {}  # repo-rel path → [import FQCNs]

    for java_file in java_files:
        try:
            source = java_file.read_bytes()
        except OSError as e:
            log.warning("Cannot read %s: %s", java_file, e)
            continue

        package, class_name, imports = _extract_java_metadata(source)
        if not class_name:
            log.debug("No class name found in %s, skipping", java_file)
            continue

        fqcn = f"{package}.{class_name}" if package else class_name
        rel_path = str(java_file.relative_to(repo_path))
        fqcn_to_path[fqcn] = rel_path
        file_imports[rel_path] = imports

    # Pass 2: build dependency edges
    graph = DependencyGraph()
    for rel_path in file_imports:
        graph.add_file(rel_path)

    for rel_path, imports in file_imports.items():
        for imp in imports:
            # exact FQCN match
            if imp in fqcn_to_path:
                dep_path = fqcn_to_path[imp]
                if dep_path != rel_path:
                    graph.add_edge(rel_path, dep_path)
                continue
            # wildcard import: com.example.service.* — match any file in that package
            if imp.endswith(".*"):
                pkg_prefix = imp[:-2]
                for fqcn, dep_path in fqcn_to_path.items():
                    if fqcn.startswith(pkg_prefix + ".") and dep_path != rel_path:
                        graph.add_edge(rel_path, dep_path)

    log.info(
        "Built dep graph: %d files, %d edges",
        len(graph.files()),
        sum(len(v) for v in graph._edges.values()),
    )
    return graph


# ── file task builder ─────────────────────────────────────────────────────────

def build_file_tasks(
    graph: DependencyGraph,
    rule_index: "RuleIndex",  # type: ignore[name-defined]
    repo_path: Path,
    source_glob: str = "**/*.java",
) -> list["FileTask"]:
    """Map topological order + rule index to a list of FileTask dicts."""
    order = graph.topological_order()
    tasks: list[FileTask] = []
    for path in order:
        abs_path = repo_path / path
        try:
            source = abs_path.read_text(errors="replace")
        except OSError:
            source = ""

        # Extract imports list for rule matching
        try:
            source_bytes = abs_path.read_bytes()
            _, _, imports = _extract_java_metadata(source_bytes)
        except Exception:
            imports = []

        rules = rule_index.rules_for_file(imports, source)
        tasks.append(
            {
                "path": path,
                "rules": rules,
                "deps": graph.dependencies_of(path),
            }
        )
    return tasks
