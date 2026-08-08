from __future__ import annotations

from cpip.resolution.api import ResolutionConfig


def test_resolution_config_is_immutable() -> None:
    config = ResolutionConfig(find_links=("/wheels",), constraints=("demo<2",))

    assert config.find_links == ("/wheels",)
    assert config.constraints == ("demo<2",)
