"""Simple API page parsing: the link URL join fast path and the JSON loop."""

from __future__ import annotations

import random
import urllib.parse

import pytest
from cpip.index.page_parsing import IndexPageParser, join_index_url

BASES = (
    "https://pypi.org/simple/pkg/",
    "http://example.invalid/simple/",
    "file:///srv/index/",
    "https://u:p@h:8443/a/b",
    "https://pypi.org/simple/pkg",
    "https://h",
    "https://h/",
    "http://h:8080/a/b/c/",
    "https://h/a//b/",
    "https://h/a/./b/../c/",
    "https://h/x?q",
    "https://h/x#f",
    "https://h/x;p/",
    "https:///x/",
    "https://[::1]/a/",
    "HTTPS://X/",
    "ftp://h/",
    "",
)

# Every shape that decides whether ``urljoin`` returns an absolute URL
# unchanged: clean absolute URLs, each delimiter-with-empty-component
# variant, scheme casing, whitespace and control characters, empty netlocs,
# bracketed hosts (valid and malformed), and relative references.
HREFS = (
    "https://files.pythonhosted.org/packages/a/b/c-1.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/c-1.0.tar.gz#sha256=abc",
    "https://h/x#egg=foo&sha256=1",
    "https://h/x?a=1&b=2",
    "https://h/x%20y",
    "https://user@h/x",
    "https://h:80/x",
    "https://h",
    "https://h/",
    "https://h?",
    "https://h#",
    "https://h/;",
    "https://h/a;b",
    "https://h/a;b/c",
    "https://h/a;b;c",
    "https://h/a;;",
    "https://h/a;?q",
    "https://h/a;#f",
    "https://h/a?#f",
    "https://h/a#?",
    "https://h/a#f?x",
    "https://h/a?q#",
    "https://h/a?q;",
    "https://h/a?;",
    "https://h/a#;",
    "https://h/a?b?c",
    "https://h/a##b",
    "https://h/a;b?c#d",
    "https://h/a?c;d#e;f",
    "https://h/a#b;c?d",
    "https://h;x/y",
    "https://h;",
    "https://h/a/../b",
    "https://h/./a",
    "https://h//a",
    "https://h/a//b",
    "https://h/x\\y",
    "https:///x",
    "https://?x",
    "https://#x",
    "https://",
    "http://",
    "https:/x",
    "https:x",
    "https:",
    "https",
    "HTTPS://h/x",
    "Https://h/x",
    "https://h/x y",
    "https://h/x\ty",
    "https://h/x\ny",
    "https://h/x\ry",
    " https://h/x",
    "https://h/x ",
    "https://h/\x7f",
    "https://h/\x00",
    "https://h/é",
    "https://[::1]/x",
    "https://[::1/x",
    "https://]/x",
    "https://[::1]:8080/x",
    "https://[zz]/x",
    "http://h/a;?",
    "http://h/a;#",
    "http://h/a?#",
    "http://h/a;",
    "http://h/a?",
    "http://h/a#",
    "http://h",
    "../a.whl",
    "../../packages/x-1.0-py3-none-any.whl",
    "../../packages/x.whl#sha256=abc",
    "a.whl",
    "./a.whl",
    "../",
    "..",
    ".",
    "./",
    "../..",
    "../../../../../../x",
    "/abs/x",
    "/",
    "//cdn/a.whl",
    "///x",
    "////x",
    "a//b",
    "a///b",
    "a/./b/../c",
    "a/b/",
    "a/..",
    "a/.",
    "a/../..",
    "...",
    "..a",
    "a..",
    ".hidden",
    "x#",
    "x#a#b",
    "x#?q",
    "x?q",
    "x;p",
    "x:y",
    "mailto:x",
    " x",
    "x ",
    "x\ty",
    "x\ny",
    "é",
    "a\\b",
    "%20x",
    "a%2F..%2Fb",
    "?q",
    "?",
    "#f",
    "#",
    "",
)


def _outcome(join, base: str, href: str) -> tuple[str, object]:
    try:
        return ("ok", join(base, href))
    except ValueError as exc:
        return ("error", type(exc))


@pytest.mark.parametrize("href", HREFS)
@pytest.mark.parametrize("base", BASES)
def test_join_index_url_matches_urljoin(base: str, href: str) -> None:
    assert _outcome(join_index_url, base, href) == _outcome(
        urllib.parse.urljoin,
        base,
        href,
    )


def test_join_index_url_matches_urljoin_on_random_inputs() -> None:
    alphabet = "hs:/?#;.&=%@[] \t\n\x00\x7fAaZz09-_~éHTTPSftp\\"
    prefixes = ("https://", "http://", "HTTPS://", "https:/", "https:", "", "//")
    rng = random.Random(20260820)
    for _ in range(20_000):
        base = rng.choice(BASES)
        href = rng.choice(prefixes) + "".join(
            rng.choice(alphabet) for _ in range(rng.randint(0, 12))
        )
        assert _outcome(join_index_url, base, href) == _outcome(
            urllib.parse.urljoin,
            base,
            href,
        ), (base, href)


def test_join_index_url_matches_urljoin_on_random_relative_references() -> None:
    alphabet = "ab./#?:;% -_~\\é\tA"
    pieces = ("..", ".", "", "a", "b-1.0.whl")
    rng = random.Random(20260820)
    for _ in range(20_000):
        base = rng.choice(BASES)
        segments = [
            rng.choice(pieces)
            if rng.random() < 0.7
            else "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 5)))
            for _ in range(rng.randint(0, 6))
        ]
        href = "/".join(segments)
        if rng.random() < 0.2:
            href = "/" + href
        if rng.random() < 0.3:
            href += "#" + "".join(
                rng.choice(alphabet) for _ in range(rng.randint(0, 4))
            )
        assert _outcome(join_index_url, base, href) == _outcome(
            urllib.parse.urljoin,
            base,
            href,
        ), (base, href)


def test_join_index_url_returns_absolute_href_unchanged() -> None:
    href = "https://files.pythonhosted.org/packages/c-1.0-py3-none-any.whl"
    assert join_index_url("https://pypi.org/simple/c/", href) is href


@pytest.mark.parametrize(
    "base, href, expected",
    [
        (
            "https://pypi.org/simple/c/",
            "../../packages/c-1.0.tar.gz",
            "https://pypi.org/packages/c-1.0.tar.gz",
        ),
        (
            "https://pypi.org/simple/c/",
            "../../packages/c-1.0.whl#sha256=ab",
            "https://pypi.org/packages/c-1.0.whl#sha256=ab",
        ),
        ("https://pypi.org/simple/c", "c-1.0.whl", "https://pypi.org/simple/c-1.0.whl"),
        ("https://h", "x.whl", "https://h/x.whl"),
        ("https://h/a/", "../../../x", "https://h/x"),
        ("https://h/a/", "/abs/x", "https://h/abs/x"),
        ("https://h/a/b/", "..", "https://h/a/"),
        ("https://h/a/b/", "x#", "https://h/a/b/x"),
    ],
)
def test_join_index_url_resolves_relative_href(
    base: str,
    href: str,
    expected: str,
) -> None:
    assert join_index_url(base, href) == expected
    assert urllib.parse.urljoin(base, href) == expected


PAGE_URL = "https://example.invalid/simple/pkg/"


def test_links_from_json_reads_every_field() -> None:
    body = """{
      "meta": {"api-version": "1.1"},
      "files": [
        {
          "filename": "pkg-1.0-py3-none-any.whl",
          "url": "https://files.invalid/pkg-1.0-py3-none-any.whl",
          "hashes": {"sha256": "ab"},
          "requires-python": ">=3.9",
          "yanked": "broken",
          "core-metadata": {"sha256": "cd"},
          "upload-time": "2024-01-02T03:04:05.000000Z"
        },
        {
          "filename": "pkg-1.1.tar.gz",
          "url": "../../packages/pkg-1.1.tar.gz",
          "hashes": "not-a-dict",
          "requires-python": 3,
          "yanked": true,
          "dist-info-metadata": true
        },
        {"filename": "pkg-1.2.tar.gz", "url": "pkg-1.2.tar.gz", "yanked": false},
        {"filename": "no-url.tar.gz"},
        {"filename": "bad-url.tar.gz", "url": 7},
        "not-a-file-entry"
      ]
    }"""
    links = IndexPageParser().links_from_json(body, PAGE_URL)
    assert [link.url for link in links] == [
        "https://files.invalid/pkg-1.0-py3-none-any.whl",
        "https://example.invalid/packages/pkg-1.1.tar.gz",
        "https://example.invalid/simple/pkg/pkg-1.2.tar.gz",
    ]
    first, second, third = links
    assert first.comes_from == PAGE_URL
    assert first.text == "pkg-1.0-py3-none-any.whl"
    assert first.hashes_internal == {"sha256": "ab"}
    assert first.requires_python == ">=3.9"
    assert first.yanked_reason == "broken"
    assert first.metadata_file_data is not None
    assert first.metadata_file_data.hashes == {"sha256": "cd"}
    assert first.upload_time is not None
    assert first.upload_time.year == 2024
    assert second.hashes_internal == {}
    assert second.requires_python is None
    assert second.yanked_reason == ""
    assert second.metadata_file_data is not None
    assert second.metadata_file_data.hashes is None
    assert second.upload_time is None
    assert third.yanked_reason is None
    assert third.metadata_file_data is None


def test_links_from_html_resolves_hrefs() -> None:
    body = (
        "<html><body>"
        '<a href="https://files.invalid/pkg-1.0-py3-none-any.whl#sha256=ab">'
        "pkg-1.0-py3-none-any.whl</a>"
        '<a href="../../packages/pkg-1.1.tar.gz" data-requires-python="&gt;=3.9">'
        "pkg-1.1.tar.gz</a>"
        "</body></html>"
    )
    links = IndexPageParser().links_from_html(body, PAGE_URL)
    assert [link.url for link in links] == [
        "https://files.invalid/pkg-1.0-py3-none-any.whl#sha256=ab",
        "https://example.invalid/packages/pkg-1.1.tar.gz",
    ]
    assert links[0].hashes_internal == {"sha256": "ab"}
    assert links[1].requires_python == ">=3.9"
