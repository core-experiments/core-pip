from __future__ import annotations

import email.message
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from cpip.core.errors import InstallationError, InvalidWheelFilename
from cpip.core.packaging import Requirement, SpecifierSet
from cpip.resolution.input_paths import get_url_from_path_with_mode, looks_like_path
from cpip.resolution.input_requirements import (
    install_req_from_editable,
    install_req_from_line,
    parse_editable,
)
from cpip.resolution.req_install import InstallRequirement


def test_url_with_query_preserves_query_and_fragment() -> None:
    url = "http://foo.com/?p=bar.git;a=snapshot;h=v0.1;sf=tgz"
    fragment = "#egg=bar"
    req = install_req_from_line(url + fragment)
    assert req.link is not None
    assert req.link.url == url + fragment


def test_pep440_wheel_link_requirement() -> None:
    url = "https://whatever.com/test-0.4-py2.py3-bogus-any.whl"
    req = install_req_from_line(f"test @ {url}")
    assert req.req is not None
    parts = str(req.req).split("@", 1)
    assert parts[0].strip() == "test"
    assert parts[1].strip() == url


def test_pep440_url_link_requirement() -> None:
    url = "git+http://foo.com@ref#egg=foo"
    req = install_req_from_line(f"foo @ {url}")
    assert req.req is not None
    parts = str(req.req).split("@", 1)
    assert len(parts) == 2
    assert parts[0].strip() == "foo"
    assert parts[1].strip() == url


def test_url_with_authentication_link_requirement() -> None:
    url = "https://what@whatever.com/test-0.4-py2.py3-bogus-any.whl"
    req = install_req_from_line(url)
    assert req.link is not None
    assert req.is_wheel
    assert req.link.scheme == "https"
    assert req.link.url == url


def test_url_is_preserved_for_line_requirement() -> None:
    url = "git+http://foo.com@ref#egg=foo"
    req = install_req_from_line(url)
    assert req.link is not None
    assert req.link.url == url


def test_url_is_preserved_for_editable_requirement() -> None:
    url = "git+http://foo.com@ref#egg=foo"
    req = install_req_from_editable(url)
    assert req.link is not None
    assert req.link.url == url


def test_str_and_repr() -> None:
    req = install_req_from_line("simple==0.1")
    assert str(req) == "simple==0.1"
    assert repr(req) == "<InstallRequirement object: simple==0.1 editable=False>"


def test_requirement_identity_properties() -> None:
    pinned = install_req_from_line("demo_pkg==1.0")
    direct = install_req_from_line("demo_pkg @ https://example.com/demo.whl")

    assert pinned.name == "demo_pkg"
    assert pinned.specifier.contains("1.0")
    assert pinned.is_pinned
    assert not pinned.is_direct
    assert not pinned.has_hash_options
    assert direct.is_direct


def test_requirement_hash_options_are_owned_by_resolution() -> None:
    requirement = install_req_from_line("demo")
    requirement.hash_options["sha256"] = ["abc"]

    assert requirement.has_hash_options


def test_requirement_source_provenance_and_debug_format() -> None:
    parent = install_req_from_line("parent")
    child = install_req_from_line("child", comes_from=parent)
    from_file = install_req_from_line("other", comes_from="requirements.txt")

    assert child.from_path() == "child->parent"
    assert from_file.from_path() == "other->requirements.txt"
    assert "req=" in child.format_debug()


def test_requirement_source_paths() -> None:
    requirement = install_req_from_line("demo")
    requirement.source_dir = "/tmp/demo"

    assert requirement.unpacked_source_directory == "/tmp/demo/"
    assert requirement.setup_py_path == "/tmp/demo/setup.py"
    assert requirement.pyproject_toml_path == "/tmp/demo/pyproject.toml"


def test_invalid_wheel_requirement_raises() -> None:
    with pytest.raises(InvalidWheelFilename):
        install_req_from_line("invalid.whl")


def test_wheel_requirement_sets_req_attribute() -> None:
    req = install_req_from_line("simple-0.1-py2.py3-none-any.whl")
    assert isinstance(req.req, Requirement)
    assert str(req.req) == "simple==0.1"


@pytest.mark.parametrize(
    "line",
    [
        'mock3; python_version >= "3"',
        'mock3 ; python_version >= "3" ',
        'mock3;python_version >= "3"',
    ],
)
def test_markers(line: str) -> None:
    req = install_req_from_line(line)
    assert req.req is not None
    assert req.req.name == "mock3"
    assert str(req.req.specifier) == ""
    assert str(req.markers) == 'python_version >= "3"'


def test_markers_semicolon() -> None:
    req = install_req_from_line('semicolon; os_name == "a; b"')
    assert req.req is not None
    assert req.req.name == "semicolon"
    assert str(req.markers) == 'os_name == "a; b"'


def test_markers_invalid() -> None:
    with pytest.raises(InstallationError) as exc:
        install_req_from_line('name; python_version == "1"; python_version == "2"')
    assert "Invalid requirement" in str(exc.value)


def test_markers_url() -> None:
    url = "http://foo.com/?p=bar.git;a=snapshot;h=v0.1;sf=tgz"
    req = install_req_from_line(f'{url}; python_version >= "3"')
    assert req.link is not None
    assert req.link.url == url
    assert str(req.markers) == 'python_version >= "3"'

    req = install_req_from_line(f'{url};python_version >= "3"')
    assert req.link is not None
    assert req.link.url == f'{url};python_version >= "3"'
    assert req.markers is None


@pytest.mark.parametrize(
    "markers, matches",
    [
        ('python_version >= "1.0"', True),
        (f"sys_platform == {sys.platform!r}", True),
        ('python_version >= "5.0"', False),
        (f"sys_platform != {sys.platform!r}", False),
    ],
)
def test_markers_match(markers: str, matches: bool) -> None:
    req = install_req_from_line("name; " + markers)
    assert req.match_markers() is matches


def test_markers_match_from_line() -> None:
    for markers in (
        'python_version >= "1.0"',
        f"sys_platform == {sys.platform!r}",
    ):
        req = install_req_from_line("name; " + markers)
        assert str(req.markers) == markers
        assert req.match_markers()

    for markers in (
        'python_version >= "5.0"',
        f"sys_platform != {sys.platform!r}",
    ):
        req = install_req_from_line("name; " + markers)
        assert str(req.markers) == markers
        assert not req.match_markers()


def test_markers_match_extras_as_set() -> None:
    req = install_req_from_line('name; extra != "gpu"')
    assert req.match_markers()
    assert req.match_markers(["docs"])
    assert not req.match_markers(["gpu"])
    assert not req.match_markers(["gpu", "docs"])

    req = install_req_from_line('name; extra == "gpu"')
    assert not req.match_markers()
    assert req.match_markers(["gpu"])
    assert req.match_markers(["gpu", "docs"])

    req = install_req_from_line('name; extra == "gpu" or extra == "docs"')
    assert req.match_markers(["docs"])
    assert req.match_markers(["gpu"])
    assert not req.match_markers(["tests"])

    req = install_req_from_line('name; extra == ""')
    assert req.match_markers()
    assert not req.match_markers(["gpu"])

    req = install_req_from_line('name; extra != ""')
    assert not req.match_markers()
    assert req.match_markers(["gpu"])

    req = install_req_from_line('name; python_version >= "1" and extra != "gpu"')
    assert req.match_markers()
    assert not req.match_markers(["gpu"])


def test_markers_match_ordered_extra_comparison() -> None:
    req = install_req_from_line('name; extra >= "gpu"')
    assert not req.match_markers()
    assert req.match_markers(["gpu"])
    assert req.match_markers(["docs", "gpu"])
    assert req.match_markers(["gpu", "zzz"])


def test_markers_match_extra_in_operators() -> None:
    req = install_req_from_line('name; extra in "gpu,docs"')
    assert req.match_markers(["gpu"])
    assert req.match_markers(["docs"])
    assert not req.match_markers(["cpu"])

    req = install_req_from_line('name; extra not in "gpu,docs"')
    assert not req.match_markers(["gpu"])
    assert req.match_markers(["cpu"])
    assert not req.match_markers(["gpu", "cpu"])


def test_extras_for_non_editable_and_editable_requirements() -> None:
    req = install_req_from_line("SomeProject[ex1,ex2]")
    assert req.extras == {"ex1", "ex2"}

    req = install_req_from_line("SomeProject[ex1,ex2] @ git+https://url")
    assert req.extras == {"ex1", "ex2"}

    req = install_req_from_editable(".[ex1,ex2]")
    assert req.extras == {"ex1", "ex2"}

    req = install_req_from_editable("SomeProject[ex1,ex2] @ git+https://url")
    assert req.extras == {"ex1", "ex2"}


def test_extras_are_preserved_when_comes_from_is_present() -> None:
    comes_from = install_req_from_line("parent==1.0")

    req = install_req_from_line("SomeProject[ex1,ex2]", comes_from=comes_from)
    assert req.extras == {"ex1", "ex2"}
    assert req.comes_from is comes_from

    req = install_req_from_line(
        "SomeProject[ex1,ex2] @ git+https://url",
        comes_from=comes_from,
    )
    assert req.extras == {"ex1", "ex2"}
    assert req.comes_from is comes_from

    req = install_req_from_editable(".[ex1,ex2]", comes_from=comes_from)
    assert req.extras == {"ex1", "ex2"}
    assert req.comes_from is comes_from

    req = install_req_from_editable(
        "SomeProject[ex1,ex2] @ git+https://url",
        comes_from=comes_from,
    )
    assert req.extras == {"ex1", "ex2"}
    assert req.comes_from is comes_from


def test_unexisting_path() -> None:
    with pytest.raises(InstallationError) as exc:
        install_req_from_line(os.path.join("this", "path", "does", "not", "exist"))
    assert "Invalid requirement" in str(exc.value)
    assert "It looks like a path." in str(exc.value)


def test_single_equal_sign() -> None:
    with pytest.raises(InstallationError) as exc:
        install_req_from_line("toto=42")
    assert "= is not a valid operator. Did you mean == ?" in str(exc.value)


def test_unidentifiable_name() -> None:
    with pytest.raises(InstallationError) as exc:
        install_req_from_line("-")
    assert str(exc.value).startswith("Invalid requirement: '-'")


def test_requirement_file(tmp_path: Path) -> None:
    req_file_path = tmp_path / "test.txt"
    req_file_path.write_text("cpip\nsetuptools")
    with pytest.raises(InstallationError) as exc:
        install_req_from_line(os.fspath(req_file_path))
    error = str(exc.value)
    assert "Invalid requirement" in error
    assert "It looks like a path. The path does exist." in error
    assert "appears to be a requirements file." in error


@pytest.mark.parametrize(
    "req_str, expected",
    [
        (
            'foo[extra] @ svn+http://foo ; os_name == "nt"',
            ('foo ; os_name == "nt"', "svn+http://foo", {"extra"}),
        ),
        ("foo @ svn+http://foo", ("foo", "svn+http://foo", set())),
    ],
)
def test_parse_editable_pep508(
    req_str: str,
    expected: tuple[str, str, set[str]],
) -> None:
    assert parse_editable(req_str) == expected


def test_parse_editable_local(tmp_path: Path) -> None:
    assert parse_editable(".")[0] is None
    child = tmp_path / "foo"
    child.mkdir()
    with mock.patch("cpip.resolution.req_install.os.path.exists", return_value=True):
        with mock.patch(
            "cpip.resolution.req_install.os.path.abspath",
            return_value=os.fspath(child),
        ):
            assert parse_editable("foo") == (None, child.resolve().as_uri(), set())


def test_parse_editable_explicit_vcs() -> None:
    assert parse_editable("svn+https://foo#egg=foo") == (
        "foo",
        "svn+https://foo#egg=foo",
        set(),
    )


def test_parse_editable_vcs_extras() -> None:
    assert parse_editable("foo[extras] @ svn+https://foo") == (
        "foo",
        "svn+https://foo",
        {"extras"},
    )


def test_parse_editable_local_extras(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child = root / "foo"
    child.mkdir()
    with (
        mock.patch(
            "cpip.resolution.req_install.os.path.abspath",
            return_value=os.fspath(root),
        ),
        mock.patch(
            "cpip.resolution.req_install.os.path.exists",
            return_value=True,
        ),
    ):
        assert parse_editable(".[extras]") == (
            None,
            root.resolve().as_uri(),
            {"extras"},
        )
    with (
        mock.patch(
            "cpip.resolution.req_install.os.path.abspath",
            return_value=os.fspath(child),
        ),
        mock.patch(
            "cpip.resolution.req_install.os.path.exists",
            return_value=True,
        ),
    ):
        assert parse_editable("foo[bar,baz]") == (
            None,
            child.resolve().as_uri(),
            {"bar", "baz"},
        )


def test_mismatched_versions(caplog: pytest.LogCaptureFixture) -> None:
    req = InstallRequirement(
        req=Requirement("simplewheel", SpecifierSet("==2.0"), frozenset()),
        comes_from=None,
    )
    req.source_dir = "/tmp/somewhere"
    metadata = email.message.Message()
    metadata["name"] = "simplewheel"
    metadata["version"] = "1.0"
    req.metadata_internal = metadata

    req.assert_source_matches_version()
    assert caplog.records[-1].message == (
        "Requested simplewheel==2.0, but installing version 1.0"
    )


def test_mismatched_metadata_name_is_normalized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    req = install_req_from_line("old-name==1.0")
    metadata = email.message.Message()
    metadata["name"] = "New_Name"
    req.metadata_internal = metadata

    req.warn_on_mismatching_name()

    assert req.name == "new-name"
    assert "Fix your #egg=old-name fragments." in caplog.records[-1].message


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/path/to/installable", True),
        ("./path/to/installable", True),
        (".", True),
        ("https://whatever.com/test-0.4-py2.py3-bogus-any.whl", True),
        ("test @ https://whatever.com/test-0.4-py2.py3-bogus-any.whl", True),
        ("simple-0.1-py2.py3-none-any.whl", False),
    ],
)
def test_looks_like_path(value: str, expected: bool) -> None:
    assert looks_like_path(value) == expected


@pytest.mark.parametrize(
    "args, mock_returns, expected",
    [
        (
            (
                "/path/to/foo @ git+http://foo.com@ref#egg=foo",
                "foo @ git+http://foo.com@ref#egg=foo",
            ),
            (False, False),
            None,
        ),
        (
            (
                "/path/to/foo@git+http://foo.com@ref#egg=foo",
                "foo @ git+http://foo.com@ref#egg=foo",
            ),
            (False, False),
            None,
        ),
        (
            (
                "/path/to/test @ https://whatever.com/test-0.4-py2.py3-bogus-any.whl",
                "test @ https://whatever.com/test-0.4-py2.py3-bogus-any.whl",
            ),
            (False, False),
            None,
        ),
        (("/path/to/simple==0.1", "simple==0.1"), (False, False), None),
    ],
)
def test_get_url_from_path(
    args: tuple[str, str],
    mock_returns: tuple[bool, bool],
    expected: None,
) -> None:
    with (
        mock.patch(
            "cpip.resolution.req_install.os.path.isdir",
            return_value=mock_returns[0],
        ),
        mock.patch(
            "cpip.resolution.req_install.os.path.isfile",
            return_value=mock_returns[1],
        ),
    ):
        assert get_url_from_path_with_mode(*args)[0] is expected


def test_get_url_from_path_archive_file(tmp_path: Path) -> None:
    name = "simple-0.1-py2.py3-none-any.whl"
    path = tmp_path / name
    path.touch()
    assert get_url_from_path_with_mode(str(path), name)[0] == path.resolve().as_uri()


def test_get_url_from_path_installable_dir(tmp_path: Path) -> None:
    name = "some/setuptools/project"
    path = tmp_path / name
    path.mkdir(parents=True)
    (path / "setup.py").touch()
    assert get_url_from_path_with_mode(str(path), name)[0] == path.resolve().as_uri()


def test_get_url_from_path_installable_error(tmp_path: Path) -> None:
    name = "some/setuptools/project"
    path = tmp_path / name
    path.mkdir(parents=True)
    with pytest.raises(InstallationError) as exc:
        get_url_from_path_with_mode(str(path), name)
    assert "Neither 'setup.py' nor 'pyproject.toml' found" in str(exc.value)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="no need to test symlinks on Windows",
)
def test_tmp_build_directory() -> None:
    requirement = InstallRequirement(None, None)
    tmp_dir = tempfile.mkdtemp("-build", "cpip-")
    try:
        tmp_build_dir = requirement.ensure_build_location(tmp_dir)
        assert os.path.dirname(tmp_build_dir) == os.path.realpath(
            os.path.dirname(tmp_dir),
        )
        if os.path.realpath(tmp_dir) != os.path.abspath(tmp_dir):
            assert os.path.dirname(tmp_build_dir) != os.path.dirname(tmp_dir)
        else:
            assert os.path.dirname(tmp_build_dir) == os.path.dirname(tmp_dir)
    finally:
        if os.path.isdir(tmp_dir):
            os.rmdir(tmp_dir)


def test_forward_slash_results_in_a_link(tmp_path: Path) -> None:
    install_dir = tmp_path / "foo" / "bar"
    setup_py_path = install_dir / "setup.py"
    os.makedirs(install_dir)
    setup_py_path.write_text("")
    requirement = install_req_from_line(install_dir.as_posix())
    assert requirement.link is not None


def test_load_pyproject_reads_legacy_setup_once(tmp_path: Path) -> None:
    setup_py = tmp_path / "setup.py"
    setup_py.write_text("import pkg_resources\n")
    requirement = InstallRequirement(None, None)
    requirement.source_dir = tmp_path

    with mock.patch("builtins.open", wraps=open) as open_file:
        requirement.load_pyproject_toml()

    setup_opens = [
        call
        for call in open_file.call_args_list
        if call.args and os.fspath(setup_py) in os.fspath(call.args[0])
    ]
    assert len(setup_opens) == 1
