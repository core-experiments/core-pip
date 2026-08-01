"""Benchmarks for Simple API parsing and candidate selection.

Every ``pip install`` reads one index page per project and then filters and
ranks all of its links, so these paths scale with the size of the index.
"""

from __future__ import annotations

from benchmark_support import reset_caches, wheel_filenames
from pytest_codspeed import BenchmarkFixture
from pip.core.packaging import SpecifierSet, parse_requirement
from pip.core.wheel import parse_wheel_file, supported_wheel_tags, wheel_tag_rank
from pip.index.candidate_evaluators import CandidateEvaluator
from pip.index.candidates import BestCandidateResult, InstallationCandidate
from pip.index.links import Link
from pip.index.page_parsing import IndexPageParser

PAGE_URL = "https://example.invalid/simple/package/"
WHEEL_FILENAMES = wheel_filenames()
CANDIDATE_URLS = [
    f"https://example.invalid/packages/{filename}" for filename in WHEEL_FILENAMES
]


def build_candidates() -> list[InstallationCandidate]:
    candidates = []
    for index, filename in enumerate(WHEEL_FILENAMES):
        link = Link.from_url(
            f"https://example.invalid/packages/{filename}",
            source_url=PAGE_URL,
            requires_python=">=3.9",
            yanked_reason="broken release" if index % 50 == 0 else None,
        )
        candidates.append(InstallationCandidate("package", f"1.{index}.0", link))
    return candidates


def test_parse_html_index_page(benchmark: BenchmarkFixture, index_html: str) -> None:
    parser = IndexPageParser()

    def parse_page() -> int:
        reset_caches()
        return len(parser.links_from_html(index_html, PAGE_URL))

    assert benchmark(parse_page) > 0


def test_parse_json_index_page(benchmark: BenchmarkFixture, index_json: str) -> None:
    parser = IndexPageParser()

    def parse_page() -> int:
        reset_caches()
        return len(parser.links_from_json(index_json, PAGE_URL))

    assert benchmark(parse_page) > 0


def test_build_links(benchmark: BenchmarkFixture) -> None:
    def build_all() -> int:
        reset_caches()
        return sum(
            len(Link.from_url(url, source_url=PAGE_URL).filename)
            for url in CANDIDATE_URLS
        )

    assert benchmark(build_all) > 0


def test_parse_wheel_filenames(benchmark: BenchmarkFixture) -> None:
    def parse_all() -> int:
        reset_caches()
        return sum(parse_wheel_file(name) is not None for name in WHEEL_FILENAMES)

    assert benchmark(parse_all) > 0


def test_rank_wheel_tags(benchmark: BenchmarkFixture) -> None:
    supported = supported_wheel_tags()
    parsed = [parse_wheel_file(name) for name in WHEEL_FILENAMES]
    tag_sets = [wheel.tags for wheel in parsed if wheel is not None]

    def rank_all() -> int:
        wheel_tag_rank.cache_clear()
        return sum(
            1 for tags in tag_sets if wheel_tag_rank(tags, supported) is not None
        )

    assert benchmark(rank_all) > 0


def test_evaluate_links(benchmark: BenchmarkFixture) -> None:
    requirement = parse_requirement("package>=1.100,<1.350")
    links = [
        Link.from_url(url, source_url=PAGE_URL, requires_python=">=3.9")
        for url in CANDIDATE_URLS
    ]

    def evaluate_all() -> int:
        reset_caches()
        return sum(
            isinstance(
                CandidateEvaluator.evaluate_link(
                    link,
                    requirement,
                    allow_yanked=False,
                    allow_binary=True,
                    allow_source=True,
                    target=None,
                ),
                InstallationCandidate,
            )
            for link in links
        )

    assert benchmark(evaluate_all) > 0


def test_compute_best_candidate(benchmark: BenchmarkFixture) -> None:
    candidates = build_candidates()
    evaluator = CandidateEvaluator.create(
        "package", specifier=SpecifierSet(">=1.20,<1.390")
    )

    def compute_best() -> BestCandidateResult:
        return evaluator.compute_best_candidate(candidates)

    result = benchmark(compute_best)
    assert result.best_candidate is not None
