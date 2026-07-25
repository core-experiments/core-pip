"""Static architecture checks for the canonical pip source tree."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
import tomllib


WORKSPACE_ROOT = Path(__file__).parents[1]


def _policy() -> tuple[tuple[str, ...], dict[str, set[str]]]:
    with (WORKSPACE_ROOT / "pyproject.toml").open("rb") as file:
        config = tomllib.load(file)["tool"]["pip"]["architecture"]
    return (
        tuple(config["domains"]),
        {domain: set(edges) for domain, edges in config["dependencies"].items()},
    )


def _source(domain: str) -> Path:
    return WORKSPACE_ROOT / "src" / "pip" / domain


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if len(parts) >= 2 and parts[:1] == ["pip"]:
                    imports.add(".".join(parts[:2]))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            parts = node.module.split(".")
            if len(parts) >= 2 and parts[:1] == ["pip"]:
                imports.add(".".join(parts[:2]))
    return imports


def _graph(domains: tuple[str, ...]) -> dict[str, set[str]]:
    graph = {domain: set() for domain in domains}
    for domain in domains:
        for path in _source(domain).rglob("*.py"):
            for imported in _imports(path):
                dependency = imported.removeprefix("pip.")
                if dependency in graph and dependency != domain:
                    graph[domain].add(dependency)
    return graph


def test_source_has_no_legacy_internal_tree() -> None:
    assert not (WORKSPACE_ROOT / "src/pip/_internal").exists()


def test_source_dependency_graph_is_declared_and_acyclic() -> None:
    domains, policy = _policy()
    graph = _graph(domains)
    assert set(graph) == set(policy)
    for domain, dependencies in graph.items():
        assert dependencies <= policy[domain]

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(domain: str) -> None:
        assert domain not in visiting, f"source dependency cycle at {domain}"
        if domain in visited:
            return
        visiting.add(domain)
        for dependency in graph[domain]:
            visit(dependency)
        visiting.remove(domain)
        visited.add(domain)

    for domain in domains:
        visit(domain)


def test_source_domains_are_importable() -> None:
    for domain in _policy()[0]:
        importlib.import_module(f"pip.{domain}")


def test_source_has_no_standalone_or_legacy_pip_imports() -> None:
    violations = []
    for domain in _policy()[0]:
        for path in _source(domain).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "pip._internal" in source or "pip_*" in source:
                violations.append(str(path.relative_to(WORKSPACE_ROOT)))
    assert not violations, "legacy pip imports in source: " + ", ".join(violations)
