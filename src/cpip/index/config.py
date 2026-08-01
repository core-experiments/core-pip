"""Configuration defaults for package discovery."""

import urllib.parse

DEFAULT_INDEX_URL = "https://pypi.org/simple"


class PackageIndex:
    __slots__ = ("file_storage_domain", "url")

    def __init__(self, url: str, file_storage_domain: str) -> None:
        self.url = url
        self.file_storage_domain = file_storage_domain

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
