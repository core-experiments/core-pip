"""Offline benchmarks over uv's real-world compiled lockfile corpus."""

from __future__ import annotations

from pathlib import Path

from benchmark_support import reset_caches
from cpip.core.packaging import parse_requirement
from pytest_codspeed import BenchmarkFixture

CORPUS_DIR = Path(__file__).with_name("corpus") / "uv_workloads"


def load_corpus() -> dict[str, list[str]]:
    corpus = {}
    for path in sorted(CORPUS_DIR.glob("*.txt")):
        lines = [
            stripped
            for line in path.read_text(encoding="utf-8").splitlines()
            if (stripped := line.strip()) and not stripped.startswith("#")
        ]
        corpus[path.name] = lines
    return corpus


def test_uv_corpus_requirement_parsing(benchmark: BenchmarkFixture) -> None:
    corpus = load_corpus()
    total_lines = sum(len(lines) for lines in corpus.values())

    def parse_corpus() -> int:
        reset_caches()
        count = 0
        for lines in corpus.values():
            for requirement in lines:
                parse_requirement(requirement)
                count += 1
        return count

    assert benchmark(parse_corpus) == total_lines
