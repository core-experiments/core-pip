import pytest
from cpip.core.packaging import SpecifierSet, parse_requirement
from cpip.core.versions import Version
from cpip.core.wheel import WheelTag
from cpip.index.candidate_evaluators import CandidateEvaluator
from cpip.index.candidates import InstallationCandidate
from cpip.index.links import Link
from cpip.index.source_models import CandidateRecord, RejectedCandidate


def test_sort_key_uses_best_supported_tag_rank() -> None:
    evaluator = CandidateEvaluator(
        "demo-pkg",
        supported_tags=[
            WheelTag("py3", "none", "any"),
            WheelTag("py2", "none", "any"),
        ],
        specifier=SpecifierSet(),
    )
    candidate = InstallationCandidate(
        "demo-pkg",
        "1.0",
        Link.from_url(
            "https://example.invalid/demo_pkg-1.0-py2.py3-none-any.whl",
            source_url=None,
        ),
    )

    assert evaluator.sort_key_internal(candidate)[6] == 0


def test_sort_key_reuses_preparsed_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = Link.from_url(
        "https://example.invalid/demo_pkg-1.0-py3-none-any.whl",
        source_url=None,
    )
    candidate = InstallationCandidate.from_link(link)
    assert isinstance(candidate, InstallationCandidate)
    evaluator = CandidateEvaluator(
        "demo-pkg",
        supported_tags=[WheelTag("py3", "none", "any")],
        specifier=SpecifierSet(),
    )

    def fail_wheel_parse(filename: str) -> None:
        raise AssertionError(f"reparsed wheel filename: {filename}")

    monkeypatch.setattr("cpip.index.candidate_evaluators.Wheel", fail_wheel_parse)

    assert evaluator.sort_key_internal(candidate)[6] == 0


def test_sort_key_marks_unsupported_wheel_tag() -> None:
    evaluator = CandidateEvaluator(
        "demo-pkg",
        supported_tags=[WheelTag("py3", "none", "any")],
        specifier=SpecifierSet(),
    )
    candidate = InstallationCandidate(
        "demo-pkg",
        "1.0",
        Link.from_url(
            "https://example.invalid/demo_pkg-1.0-py2-none-any.whl",
            source_url=None,
        ),
    )

    assert evaluator.sort_key_internal(candidate)[6] == -1_000_000


def test_unnamed_direct_archive_uses_materializable_record() -> None:
    link = Link.from_url(
        "https://example.invalid/archive/master.zip",
        source_url=None,
    )
    parsed = InstallationCandidate.from_link(link)
    assert isinstance(parsed, RejectedCandidate)
    requirement = parse_requirement(f"source @ {link.url}")
    assert requirement is not None

    candidate = CandidateEvaluator.evaluate_parsed_link(
        link,
        parsed,
        requirement,
        allow_yanked=True,
        allow_binary=True,
        allow_source=True,
    )

    assert type(candidate) is CandidateRecord
    assert candidate.version == Version("0")
