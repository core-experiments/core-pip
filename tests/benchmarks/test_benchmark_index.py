"""Benchmarks for Simple API parsing and candidate selection.

Every ``pip install`` reads one index page per project and then filters and
ranks all of its links, so these paths scale with the size of the index.
"""

from __future__ import annotations

import json
from pathlib import Path

from benchmark_support import (
    reset_caches,
    simple_index_html,
    simple_index_json,
    wheel_filenames,
)
from pytest_codspeed import BenchmarkFixture
from cpip.core.packaging import SpecifierSet, parse_requirement
from cpip.core.wheel import (
    TargetContext,
    parse_wheel_file,
    supported_wheel_tags,
    wheel_tag_rank,
)
from cpip.index.candidate_evaluators import CandidateEvaluator
from cpip.index.candidates import BestCandidateResult, InstallationCandidate
from cpip.index.links import Link
from cpip.index.page_parsing import IndexPageParser
from cpip.index.provider import CandidateProvider

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


def test_parse_index_fanout(benchmark: BenchmarkFixture) -> None:
    pages = tuple(
        (f"https://example.invalid/simple/package-{index}/", simple_index_html(200))
        for index in range(32)
    )
    parser = IndexPageParser()

    def parse_pages() -> int:
        reset_caches()
        return sum(len(parser.links_from_html(body, url)) for url, body in pages)

    assert benchmark(parse_pages) == 12_800


def test_parse_metadata_only_index(benchmark: BenchmarkFixture) -> None:
    body = simple_index_json(400)
    parser = IndexPageParser()

    def parse_page() -> int:
        reset_caches()
        links = parser.links_from_json(body, PAGE_URL)
        return sum(link.metadata_file is not None for link in links)

    assert benchmark(parse_page) == 400


def test_target_environment_filtering(benchmark: BenchmarkFixture) -> None:
    requirement = parse_requirement("package")
    links = [
        Link.from_url(
            f"https://example.invalid/packages/{filename}",
            source_url=PAGE_URL,
            requires_python=">=3.9",
        )
        for filename in WHEEL_FILENAMES
    ]
    targets = (
        TargetContext(platforms=("manylinux_2_17_x86_64",), python_version="3.12"),
        TargetContext(platforms=("win_amd64",), python_version="3.11"),
        TargetContext(platforms=("macosx_11_0_arm64",), python_version="3.12"),
    ) + tuple(
        TargetContext(platforms=(platform,), python_version=f"3.{version}")
        for platform in ("manylinux_2_17_x86_64", "win_amd64", "macosx_11_0_arm64")
        for version in (8, 9, 10, 11, 12, 13, 14)
    )

    def filter_targets() -> int:
        reset_caches()
        return sum(
            isinstance(
                CandidateEvaluator.evaluate_link(
                    link,
                    requirement,
                    allow_yanked=False,
                    allow_binary=True,
                    allow_source=True,
                    target=target,
                ),
                InstallationCandidate,
            )
            for target in targets
            for link in links
        )

    assert benchmark(filter_targets) > 0


def test_distribution_diversity(benchmark: BenchmarkFixture) -> None:
    names = (
        "package-2.0.0-py3-none-any.whl",
        "package-1.9.0rc1-py3-none-any.whl",
        "package-1.8.0+local.1-py3-none-any.whl",
        "package-1.7.0-cp312-cp312-manylinux_2_17_x86_64.whl",
        "package-1.6.0.tar.gz",
        "not-a-wheel.whl",
    )
    links = [
        Link.from_url(
            f"https://example.invalid/packages/{name}",
            source_url=PAGE_URL,
            yanked_reason="bad release" if index == 1 else None,
            requires_python=">=99" if index == 2 else ">=3.9",
        )
        for index, name in enumerate(names)
    ]
    requirement = parse_requirement("package>=1")

    def evaluate_diverse() -> int:
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

    assert benchmark(evaluate_diverse) > 0


def test_index_topology_ranking(benchmark: BenchmarkFixture) -> None:
    filenames = (
        "package-2.0.0.tar.gz",
        "package-2.0.0-py3-none-any.whl",
        "package-1.9.0-py3-none-any.whl",
    )
    candidates = [
        InstallationCandidate(
            "package",
            filename.split("-", 2)[1].removesuffix(".tar.gz"),
            Link.from_url(
                f"https://example.invalid/{source}/{filename}", source_url=source
            ),
        )
        for source in ("find-links", "index")
        for filename in filenames
    ]
    default = CandidateEvaluator.create("package", specifier=SpecifierSet(">=1"))
    binary = CandidateEvaluator.create(
        "package", specifier=SpecifierSet(">=1"), prefer_binary=True
    )

    def rank_sources() -> str:
        best_default = default.compute_best_candidate(candidates).best_candidate
        best_binary = binary.compute_best_candidate(candidates).best_candidate
        assert best_default is not None
        assert best_binary is not None
        return f"{best_default.version}:{best_binary.version}"

    assert benchmark(rank_sources) == "2.0.0:2.0.0"


def test_index_fallback_and_duplicate_topology(
    benchmark: BenchmarkFixture, tmp_path: Path
) -> None:
    primary = tmp_path / "primary"
    fallback = tmp_path / "fallback"
    for root in (primary, fallback):
        package = root / "topology"
        package.mkdir(parents=True)
        (package / "index.json").write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "filename": "topology-1.0.0-py3-none-any.whl",
                            "url": "https://example.invalid/topology-1.0.0.whl",
                        },
                        {
                            "filename": "topology-2.0.0-py3-none-any.whl",
                            "url": "https://example.invalid/topology-2.0.0.whl",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
    requirement = parse_requirement("topology")

    def collect_fallback() -> int:
        provider = CandidateProvider.from_options(
            find_links=[str(primary)],
            index_url=str(fallback),
            no_index=False,
        )
        links = provider.collect_links(requirement)
        provider.close()
        return len(links)

    assert benchmark(collect_fallback) == 2


def test_universal_target_matrix(benchmark: BenchmarkFixture) -> None:
    requirement = parse_requirement("package")
    links = [
        Link.from_url(
            f"https://example.invalid/packages/{filename}",
            source_url=PAGE_URL,
            requires_python=">=3.9",
        )
        for filename in WHEEL_FILENAMES
    ]
    targets = tuple(
        TargetContext(platforms=(platform,), python_version=f"3.{version}")
        for platform in (
            "manylinux_2_17_x86_64",
            "win_amd64",
            "macosx_11_0_arm64",
            "musllinux_1_2_aarch64",
        )
        for version in (9, 10, 11, 12, 13)
    )

    def evaluate_matrix() -> int:
        reset_caches()
        return sum(
            isinstance(
                CandidateEvaluator.evaluate_link(
                    link,
                    requirement,
                    allow_yanked=False,
                    allow_binary=True,
                    allow_source=True,
                    target=target,
                ),
                InstallationCandidate,
            )
            for target in targets
            for link in links
        )

    assert benchmark(evaluate_matrix) > 100


def test_prerelease_and_yanked_policy(benchmark: BenchmarkFixture) -> None:
    requirement = parse_requirement("package>=1")
    links = [
        Link.from_url(
            "https://example.invalid/packages/package-2.0.0rc1-py3-none-any.whl",
            source_url=PAGE_URL,
            yanked_reason="broken release",
        ),
        Link.from_url(
            "https://example.invalid/packages/package-1.9.0-py3-none-any.whl",
            source_url=PAGE_URL,
        ),
    ]

    def evaluate_policies() -> int:
        reset_caches()
        strict = sum(
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
        permissive = sum(
            isinstance(
                CandidateEvaluator.evaluate_link(
                    link,
                    parse_requirement("package==2.0.0rc1"),
                    allow_yanked=True,
                    allow_binary=True,
                    allow_source=True,
                    target=None,
                ),
                InstallationCandidate,
            )
            for link in links
        )
        return strict + permissive

    assert benchmark(evaluate_policies) == 2


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
