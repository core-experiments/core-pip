"""Configuration defaults for package discovery."""

import urllib.parse
from dataclasses import dataclass

DEFAULT_INDEX_URL = "https://pypi.org/simple"


@dataclass(frozen=True)
class PackageIndex:
    url: str
    file_storage_domain: str

    @property
    def netloc(self) -> str:
        return urllib.parse.urlparse(self.url).netloc

    @property
    def simple_url(self) -> str:
        return urllib.parse.urljoin(self.url, "simple")

    @property
    def pypi_url(self) -> str:
        return urllib.parse.urljoin(self.url, "pypi")


PyPI = PackageIndex("https://pypi.org/", "files.pythonhosted.org")
