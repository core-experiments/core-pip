from __future__ import annotations

import os
from pathlib import Path

from cpip.cli.requirements import collect_requirements


def test_collect_requirements_uses_http_cache_directory(tmp_path: Path) -> None:
    bundle = collect_requirements(
        requirements=["demo"],
        cache_dir=os.fspath(tmp_path),
    )

    assert bundle.session.cache.directory == os.path.join(
        os.fspath(tmp_path), "http-v1"
    )
