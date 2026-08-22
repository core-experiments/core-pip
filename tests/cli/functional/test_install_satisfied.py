"""``cpip install <name>`` for names already installed prints each
requirement once and installs nothing -- on the pre-startup recognizer and on
the normal path alike."""

from __future__ import annotations

from cpip_test_support import CpipTestEnvironment, TestData


def test_already_installed_names_are_reported_once(
    script: CpipTestEnvironment, data: TestData
) -> None:
    script.cpip_install_local("simplewheel==2.0")

    result = script.cpip("install", "--no-index", "simplewheel", "simplewheel>=1.0")
    assert result.stdout == (
        "Requirement already satisfied: simplewheel\n"
        "Requirement already satisfied: simplewheel>=1.0\n"
    ), result.stdout
    assert not result.files_created

    result = script.cpip("install", "--no-index", "-f", data.find_links, "simplewheel")
    assert result.stdout == (
        f"Looking in links: {data.find_links}\n"
        "Requirement already satisfied: simplewheel\n"
    ), result.stdout
    assert not result.files_created

    # The normal path (an option the recognizer does not take) prints the
    # same lines, once each.
    result = script.cpip(
        "install",
        "--no-index",
        "--no-binary",
        ":all:",
        "simplewheel",
        "simplewheel>=1.0",
    )
    assert result.stdout.count("Requirement already satisfied: simplewheel\n") == 1
    assert result.stdout.count("Requirement already satisfied: simplewheel>=1.0\n") == 1
    assert not result.files_created


def test_unmet_specifier_still_resolves(
    script: CpipTestEnvironment, data: TestData
) -> None:
    script.cpip_install_local("simplewheel==1.0")

    result = script.cpip(
        "install", "--no-index", "-f", data.find_links, "simplewheel>=2.0"
    )
    assert "Requirement already satisfied" not in result.stdout
    result.did_create(script.site_packages / "simplewheel-2.0.dist-info")
