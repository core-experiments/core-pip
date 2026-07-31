from __future__ import annotations

import io
import threading
import time

from cpip.network.http import HttpResponse
from cpip.resolution.requirement_files.parser import parse_requirements


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
                    b"demo==1\n"
                    if url.endswith("requirements.txt")
                    else b"demo<2\n"
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
