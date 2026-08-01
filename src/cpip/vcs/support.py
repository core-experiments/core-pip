from __future__ import annotations

import os
import urllib.parse
from collections.abc import Iterable

from cpip.core.urls import split_auth_from_netloc


class HiddenText:
    __slots__ = ("secret", "redacted")

    def __init__(self, secret: str, redacted: str) -> None:
        self.secret = secret
        self.redacted = redacted

    secret: str
    redacted: str

    def __str__(self) -> str:
        return self.redacted

    def __repr__(self) -> str:
        return f"<HiddenText {self.redacted!r}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, HiddenText):
            return NotImplemented
        return self.secret == other.secret and self.redacted == other.redacted

    __hash__ = None


def hide_value(value: str) -> HiddenText:
    return HiddenText(value, "****")


def hide_url(url: str) -> HiddenText:
    parsed = urllib.parse.urlsplit(url)
    netloc, (user, password) = split_auth_from_netloc(parsed.netloc)
    if user is None:
        redacted_netloc = netloc
    elif password is None:
        redacted_netloc = f"****@{netloc}"
    else:
        redacted_netloc = f"{urllib.parse.quote(user)}:****@{netloc}"
    redacted = urllib.parse.urlunsplit(
        (parsed.scheme, redacted_netloc, parsed.path, parsed.query, parsed.fragment)
    )
    return HiddenText(url, redacted)


def is_installable_dir(path: str) -> bool:
    return os.path.isdir(path) and (
        os.path.isfile(os.path.join(path, "pyproject.toml"))
        or os.path.isfile(os.path.join(path, "setup.py"))
    )


def ask_path_exists(message: str, options: Iterable[str]) -> str:
    for action in os.environ.get("CPIP_EXISTS_ACTION", "").split():
        if action in options:
            return action
    while True:
        if os.environ.get("CPIP_NO_INPUT"):
            raise RuntimeError(f"No input was expected; question: {message}")
        response = input(message).strip().lower()
        if response in options:
            return response
        print(
            f"Your response ({response!r}) was not one of the expected responses: {', '.join(options)}"
        )
