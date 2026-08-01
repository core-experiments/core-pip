from __future__ import annotations

from types import SimpleNamespace

from cpip.resolution.engine import ResolutionConfig, ResolutionResult


def test_resolution_result_normalizes_artifacts_without_losing_source_objects() -> None:
    candidate = SimpleNamespace(
        name="Demo",
        canonical_name="demo",
        version="1.0",
        source_url="https://example.invalid/demo.whl",
        source_kind="wheel",
        requires_python=None,
        dependencies=(),
    )
    plan = SimpleNamespace(
        candidates=[candidate],
        graph={"demo": {"dependency"}},
        conflicts=["example conflict"],
        satisfied=[],
    )

    result = ResolutionResult.from_plan(plan)

    assert result.candidates == (candidate,)
    assert result.candidate_artifacts() == (candidate,)
    assert result.normalized_candidates[0].canonical_name == "demo"
    assert result.graph["demo"] == frozenset({"dependency"})
    assert result.conflicts == ("example conflict",)


def test_resolution_config_is_immutable() -> None:
    config = ResolutionConfig(find_links=("/wheels",), constraints=("demo<2",))

    assert config.find_links == ("/wheels",)
    assert config.constraints == ("demo<2",)
