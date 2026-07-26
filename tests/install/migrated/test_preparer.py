import os
import shutil
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from typing import Any
from unittest.mock import Mock, patch

import pytest
from pip.build.metadata import MetadataDistribution
from pip.core.errors import HashMismatch
from pip.core.hashes import Hashes
from pip.index.links import Link
from pip.install.downloads import DownloadManager
from pip.install.metadata import (
    MetadataInvalid,
    SidecarMetadataInconsistent,
    check_sidecar_matches_wheel,
)
from pip.network.download import Downloader
from pip.network.http import NetworkSession
from pip_test_support import TestData
from pip_test_support.requests_mocks import MockResponse
from pip.network.http import HttpResponse


def test_unpack_url_with_urllib_response_without_content_type(data: TestData) -> None:
    """
    It should download and unpack files even if no Content-Type header exists
    """
    real_session = NetworkSession()

    def fake_session_get(*args: Any, **kwargs: Any) -> HttpResponse:
        resp = real_session.get(*args, **kwargs)
        del resp.headers["Content-Type"]
        return resp

    session = Mock()
    session.resume_retries = 0
    session.get = fake_session_get
    download = Downloader(session, progress_bar="on")

    uri = data.packages.joinpath("simple-1.0.tar.gz").as_uri()
    link = Link(uri)
    temp_dir = mkdtemp()
    try:
        DownloadManager(download).unpack(link, temp_dir, verbosity=0)
        assert set(os.listdir(temp_dir)) == {
            "PKG-INFO",
            "setup.cfg",
            "setup.py",
            "simple",
            "simple.egg-info",
        }
    finally:
        rmtree(temp_dir)


@patch("pip.network.download.raise_for_status")
def test_download_http_url__no_directory_traversal(
    mock_raise_for_status: Mock, tmp_path: Path
) -> None:
    """
    Test that directory traversal doesn't happen on download when the
    Content-Disposition header contains a filename with a ".." path part.
    """
    mock_url = "http://www.example.com/whatever.tgz"
    contents = b"downloaded"
    link = Link(mock_url)

    session = Mock()
    session.resume_retries = 0
    resp = MockResponse(contents)
    resp.url = mock_url
    resp.headers.update(
        {
            # Set the content-type to a random value to prevent
            # mimetypes.guess_extension from guessing the extension.
            "content-type": "random",
            "content-disposition": 'attachment;filename="../out_dir_file"',
        }
    )
    session.get.return_value = resp
    download = Downloader(session, progress_bar="on")

    download_dir = os.fspath(tmp_path.joinpath("download"))
    os.mkdir(download_dir)
    file_path, content_type = download(link, download_dir)
    # The file should be downloaded to download_dir.
    actual = os.listdir(download_dir)
    assert actual == ["out_dir_file"]
    mock_raise_for_status.assert_called_once_with(resp)


@pytest.fixture
def clean_project(tmp_path_factory: pytest.TempPathFactory, data: TestData) -> Path:
    tmp_path = tmp_path_factory.mktemp("clean_project")
    new_project_dir = tmp_path.joinpath("FSPkg")
    path = data.packages.joinpath("FSPkg")
    shutil.copytree(path, new_project_dir)
    return new_project_dir


class Test_unpack_url:
    def prep(self, tmp_path: Path, data: TestData) -> None:
        self.build_dir = os.fspath(tmp_path.joinpath("build"))
        self.download_dir = tmp_path.joinpath("download")
        os.mkdir(self.build_dir)
        os.mkdir(self.download_dir)
        self.dist_file = "simple-1.0.tar.gz"
        self.dist_file2 = "simple-2.0.tar.gz"
        self.dist_path = data.packages.joinpath(self.dist_file)
        self.dist_path2 = data.packages.joinpath(self.dist_file2)
        self.dist_url = Link(self.dist_path.as_uri())
        self.dist_url2 = Link(self.dist_path2.as_uri())
        self.no_download = Mock(side_effect=AssertionError)

    def test_unpack_url_no_download(self, tmp_path: Path, data: TestData) -> None:
        self.prep(tmp_path, data)
        DownloadManager(self.no_download).unpack(
            self.dist_url, self.build_dir, verbosity=0
        )
        assert os.path.isdir(os.path.join(self.build_dir, "simple"))
        assert not os.path.isfile(os.path.join(self.download_dir, self.dist_file))

    def test_unpack_url_bad_hash(self, tmp_path: Path, data: TestData) -> None:
        """
        Test when the file url hash fragment is wrong
        """
        self.prep(tmp_path, data)
        url = f"{self.dist_url.url}#md5=bogus"
        dist_url = Link(url)
        with pytest.raises(HashMismatch):
            DownloadManager(self.no_download).unpack(
                dist_url,
                self.build_dir,
                hashes=Hashes({"md5": ["bogus"]}),
                verbosity=0,
            )


def metadata_internal(*lines: str, name: str = "pkg", version: str = "1.0") -> str:
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {name}",
        f"Version: {version}",
        *lines,
    ]
    return "\n".join(metadata) + "\n"


def make_distribution(metadata: str) -> MetadataDistribution:
    return MetadataDistribution.from_metadata_file_contents(
        metadata.encode("utf-8"), "pkg"
    )


class TestCheckSidecarMatchesWheel:
    """Exercise :func:`check_sidecar_matches_wheel` for each of the
    fields it cross-checks between a PEP 658 sidecar and a downloaded wheel.
    """

    def req_internal(self) -> Mock:
        # The helper only uses the ``req`` argument to build the resulting
        # exception, so a stand-in object is enough.
        return Mock()

    def test_matching_metadata_does_not_raise(self) -> None:
        dist = make_distribution(
            metadata_internal(
                "Requires-Python: >=3.9",
                "Requires-Dist: requests>=2.0",
                "Provides-Extra: extra",
            )
        )
        check_sidecar_matches_wheel(self.req_internal(), dist, dist)

    def test_requires_dist_canonicalization_is_tolerated(self) -> None:
        sidecar = make_distribution(metadata_internal("Requires-Dist: Requests >= 2.0"))
        wheel = make_distribution(metadata_internal("Requires-Dist: requests>=2.0"))
        check_sidecar_matches_wheel(self.req_internal(), sidecar, wheel)

    def test_folded_requires_dist_header_is_tolerated(self) -> None:
        # For a folded Requires-Dist header, the email parser preserves a
        # leading newline in the raw value on Python versions without the
        # python/cpython#124452 fix (3.10, 3.11, <3.12.8, 3.13.0). The check
        # must strip it, like iter_dependencies() does.
        dist = make_distribution(
            metadata_internal(
                "Requires-Dist:",
                " some-package-with-a-very-long-name[extra-one]>=2.31.0,<3.0.0",
            )
        )
        check_sidecar_matches_wheel(self.req_internal(), dist, dist)

    def test_requires_dist_mismatch_raises(self) -> None:
        sidecar = make_distribution(metadata_internal("Requires-Dist: shadow-pkg"))
        wheel = make_distribution(metadata_internal())
        with pytest.raises(SidecarMetadataInconsistent) as excinfo:
            check_sidecar_matches_wheel(self.req_internal(), sidecar, wheel)
        assert excinfo.value.field == "Requires-Dist"
        assert excinfo.value.f_val == "shadow-pkg"
        assert excinfo.value.m_val == ""

    def test_requires_dist_diff_reports_only_differences(self) -> None:
        sidecar = make_distribution(
            metadata_internal(
                "Requires-Dist: shared-a",
                "Requires-Dist: shared-b",
                "Requires-Dist: only-in-sidecar",
            )
        )
        wheel = make_distribution(
            metadata_internal(
                "Requires-Dist: shared-a",
                "Requires-Dist: shared-b",
                "Requires-Dist: only-in-wheel",
            )
        )
        with pytest.raises(SidecarMetadataInconsistent) as excinfo:
            check_sidecar_matches_wheel(self.req_internal(), sidecar, wheel)
        assert excinfo.value.field == "Requires-Dist"
        assert excinfo.value.f_val == "only-in-sidecar"
        assert excinfo.value.m_val == "only-in-wheel"

    def test_requires_python_mismatch_raises(self) -> None:
        sidecar = make_distribution(metadata_internal("Requires-Python: >=3.9"))
        wheel = make_distribution(metadata_internal())
        with pytest.raises(SidecarMetadataInconsistent) as excinfo:
            check_sidecar_matches_wheel(self.req_internal(), sidecar, wheel)
        assert excinfo.value.field == "Requires-Python"
        assert excinfo.value.f_val == ">=3.9"
        assert excinfo.value.m_val == ""

    def test_provides_extra_mismatch_raises(self) -> None:
        sidecar = make_distribution(metadata_internal("Provides-Extra: extra"))
        wheel = make_distribution(metadata_internal())
        with pytest.raises(SidecarMetadataInconsistent) as excinfo:
            check_sidecar_matches_wheel(self.req_internal(), sidecar, wheel)
        assert excinfo.value.field == "Provides-Extra"
        assert excinfo.value.f_val == "extra"
        assert excinfo.value.m_val == ""

    def test_name_mismatch_raises(self) -> None:
        sidecar = make_distribution(metadata_internal(name="other-pkg"))
        wheel = make_distribution(metadata_internal(name="pkg"))
        with pytest.raises(SidecarMetadataInconsistent) as excinfo:
            check_sidecar_matches_wheel(self.req_internal(), sidecar, wheel)
        assert excinfo.value.field == "Name"
        assert excinfo.value.f_val == "other-pkg"
        assert excinfo.value.m_val == "pkg"

    def test_name_canonicalization_is_tolerated(self) -> None:
        sidecar = make_distribution(metadata_internal(name="Pkg_Name"))
        wheel = make_distribution(metadata_internal(name="pkg-name"))
        check_sidecar_matches_wheel(self.req_internal(), sidecar, wheel)

    def test_version_mismatch_raises(self) -> None:
        sidecar = make_distribution(metadata_internal(version="1.0"))
        wheel = make_distribution(metadata_internal(version="2.0"))
        with pytest.raises(SidecarMetadataInconsistent) as excinfo:
            check_sidecar_matches_wheel(self.req_internal(), sidecar, wheel)
        assert excinfo.value.field == "Version"
        assert excinfo.value.f_val == "1.0"
        assert excinfo.value.m_val == "2.0"

    def test_version_normalization_is_tolerated(self) -> None:
        sidecar = make_distribution(metadata_internal(version="1.0"))
        wheel = make_distribution(metadata_internal(version="1.0.0"))
        check_sidecar_matches_wheel(self.req_internal(), sidecar, wheel)

    def test_invalid_requires_dist_raises_metadata_invalid(self) -> None:
        sidecar = make_distribution(
            metadata_internal("Requires-Dist: not a valid requirement")
        )
        wheel = make_distribution(metadata_internal())
        with pytest.raises(MetadataInvalid):
            check_sidecar_matches_wheel(self.req_internal(), sidecar, wheel)
