from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest
from cpip.core.errors import InstallationError
from cpip.network.http import HttpResponse
from cpip.resolution.files.models import RequirementsFileParseError
from cpip.resolution.files.parser import parse_requirements


def test_deep_requirement_includes_without_recursion(tmp_path: Path) -> None:
    count = 1_500
    for index in range(count):
        path = tmp_path / f"requirements-{index}.txt"
        content = (
            f"-r requirements-{index + 1}.txt\n" if index < count - 1 else "demo==1\n"
        )
        path.write_text(content, encoding="utf-8")

    results = parse_requirements(str(tmp_path / "requirements-0.txt"), object())

    assert [item.requirement for item in results] == ["demo==1"]


def test_cyclic_requirement_includes_have_a_bounded_failure(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("-r second.txt\n", encoding="utf-8")
    second.write_text("-r first.txt\n", encoding="utf-8")

    with pytest.raises(RequirementsFileParseError, match="recursively references"):
        parse_requirements(str(first), object())


def test_requirement_include_order_is_preserved(tmp_path: Path) -> None:
    included = tmp_path / "included.txt"
    root = tmp_path / "requirements.txt"
    included.write_text("included==1\n", encoding="utf-8")
    root.write_text("-r included.txt\nroot==1\n", encoding="utf-8")

    results = parse_requirements(str(root), object())

    assert [item.requirement for item in results] == ["included==1", "root==1"]


def test_repeated_requirement_include_reuses_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    included = tmp_path / "included.txt"
    root = tmp_path / "requirements.txt"
    included.write_text("included==1\n", encoding="utf-8")
    root.write_text("-r included.txt\n-r included.txt\n", encoding="utf-8")

    original_open = open
    reads = 0

    def counting_open(*args: object, **kwargs: object):
        nonlocal reads
        if args and args[0] == str(included):
            reads += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    results = parse_requirements(str(root), object())

    assert [item.requirement for item in results] == ["included==1", "included==1"]
    assert reads == 1


def test_simple_requirement_fast_path_retains_validation(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "demo[extra]>=1,<3; python_version >= '3.11'\n",
        encoding="utf-8",
    )

    results = parse_requirements(str(requirements), object())

    assert [item.requirement for item in results] == [
        "demo[extra]>=1,<3; python_version >= '3.11'",
    ]


def test_simple_requirement_fast_path_rejects_multiple_markers(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        'demo; python_version >= "3"; os_name == "posix"\n',
        encoding="utf-8",
    )

    with pytest.raises(InstallationError, match="Invalid requirement"):
        parse_requirements(str(requirements), object())


def test_remote_requirement_includes_are_prefetched(tmp_path) -> None:
    root = tmp_path / "requirements.txt"
    root.write_text(
        "-r https://example.test/requirements.txt\n"
        "-c https://example.test/constraints.txt\n",
        encoding="utf-8",
    )

    class Session:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.maximum = 0
            self.calls: list[str] = []

        def get(self, url: str) -> HttpResponse:
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.calls.append(url)
            try:
                time.sleep(0.05)
                content = (
                    b"demo==1\n" if url.endswith("requirements.txt") else b"demo<2\n"
                )
                return HttpResponse(
                    status_code=200,
                    reason="OK",
                    url=url,
                    headers={"Content-Type": "text/plain"},
                    raw=io.BytesIO(content),
                )
            finally:
                with self.lock:
                    self.active -= 1

    session = Session()
    results = parse_requirements(str(root), session)

    assert [item.requirement for item in results] == ["demo==1", "demo<2"]
    assert set(session.calls) == {
        "https://example.test/requirements.txt",
        "https://example.test/constraints.txt",
    }
    assert session.maximum == 2
