from __future__ import annotations

from cpip.resolution.algorithms import topological_weights


def test_topological_weights_handles_deep_chain() -> None:
    size = 4_000
    graph = {"<root>": {"node-0"}}
    graph.update({f"node-{index}": {f"node-{index + 1}"} for index in range(size - 1)})
    graph[f"node-{size - 1}"] = set()

    weights = topological_weights(graph, set(graph) - {"<root>"})

    assert weights["node-0"] == size
    assert weights[f"node-{size - 1}"] == 1


def test_topological_weights_breaks_cycles() -> None:
    graph = {
        "<root>": {"a"},
        "a": {"b"},
        "b": {"a", "c"},
        "c": set(),
    }

    weights = topological_weights(graph, {"a", "b", "c"})

    assert set(weights) == {"a", "b", "c"}
    assert weights["c"] == 1
    assert all(1 <= weights[name] <= 3 for name in ("a", "b"))
