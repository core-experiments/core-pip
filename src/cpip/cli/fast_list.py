"""Fast path for explicit-path package listing."""

from __future__ import annotations

import os


class ListOptions:
    __slots__ = ("excludes", "format", "paths", "verbose")

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.format = "columns"
        self.verbose = 0
        self.excludes: set[str] = set()


UNSUPPORTED_FLAGS = frozenset(
    (
        "-o",
        "--outdated",
        "-u",
        "--uptodate",
        "-e",
        "--editable",
        "-l",
        "--local",
        "--user",
        "--not-required",
        "--find-links",
        "-f",
        "--index-url",
        "-i",
        "--extra-index-url",
        "--no-index",
        "--pre",
        "--all-releases",
        "--only-final",
        "--exclude-editable",
        "--include-editable",
    ),
)


def canonicalize_name(value: str) -> str:
    return value.replace("_", "-").lower()


def option_value(args: list[str], index: int) -> str | None:
    if index + 1 >= len(args):
        return None
    return args[index + 1]


def parse_arguments(args: list[str]) -> ListOptions | None:
    options = ListOptions()
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("--path", "--format", "--exclude"):
            value = option_value(args, index)
            if value is None:
                return None
            if token == "--path":
                options.paths.append(value)
            elif token == "--format":
                if value not in {"columns", "json", "freeze"}:
                    return None
                options.format = value
            else:
                options.excludes.add(canonicalize_name(value))
            index += 2
            continue
        if token.startswith("--path="):
            options.paths.append(token.partition("=")[2])
        elif token.startswith("--format="):
            value = token.partition("=")[2]
            if value not in {"columns", "json", "freeze"}:
                return None
            options.format = value
        elif token.startswith("--exclude="):
            options.excludes.add(canonicalize_name(token.partition("=")[2]))
        elif token in ("-v", "--verbose"):
            options.verbose += 1
        elif token.startswith("-") and len(token) > 1 and set(token[1:]) == {"v"}:
            options.verbose += len(token) - 1
        elif token in UNSUPPORTED_FLAGS or token.startswith("-"):
            return None
        else:
            return None
        index += 1
    if not options.paths:
        return None
    return options


def parse_metadata(path: str) -> dict[str, str] | None:
    metadata_path = os.path.join(path, "METADATA")
    try:
        with open(metadata_path, encoding="utf-8") as file:
            lines = file.read().splitlines()
    except OSError:
        return None
    result: dict[str, str] = {}
    for line in lines:
        if not line:
            break
        key, separator, value = line.partition(":")
        if separator:
            result[key.lower()] = value.strip()
    if not result.get("name") or not result.get("version"):
        return None
    return result


def iter_distributions(paths: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for root in paths:
        try:
            with os.scandir(root) as entries:
                entry_names = [
                    entry.name
                    for entry in entries
                    if entry.is_dir()
                    and (
                        entry.name.endswith(".dist-info")
                        or entry.name.endswith(".egg-info")
                    )
                ]
        except OSError:
            continue
        for name in sorted(entry_names):
            metadata = parse_metadata(os.path.join(root, name))
            if metadata is None:
                continue
            metadata["location"] = root
            result.append(metadata)
    return result


def json_string(value: str) -> str:
    replacements = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    parts = ['"']
    for char in value:
        replacement = replacements.get(char)
        if replacement is not None:
            parts.append(replacement)
        elif ord(char) < 0x20:
            parts.append(f"\\u{ord(char):04x}")
        else:
            parts.append(char)
    parts.append('"')
    return "".join(parts)


def render_json(distributions: list[dict[str, str]], verbose: bool) -> str:
    if not distributions:
        return "[]"
    items = []
    for distribution in distributions:
        fields = [
            f'"name": {json_string(distribution["name"])}',
            f'"version": {json_string(distribution["version"])}',
        ]
        if verbose:
            fields.append(f'"location": {json_string(distribution["location"])}')
        items.append("{" + ", ".join(fields) + "}")
    return "[" + ", ".join(items) + "]"


def run(args: list[str]) -> int | None:
    options = parse_arguments(args)
    if options is None:
        return None
    distributions = [
        distribution
        for distribution in iter_distributions(options.paths)
        if canonicalize_name(distribution["name"]) not in options.excludes
    ]
    distributions.sort(key=lambda item: canonicalize_name(item["name"]))

    if options.format == "json":
        print(render_json(distributions, options.verbose > 0))
        return 0
    if options.format == "freeze":
        for distribution in distributions:
            print(f"{distribution['name']}=={distribution['version']}")
        return 0

    rows = [["Package", "Version"]]
    rows.extend(
        [distribution["name"], distribution["version"]]
        for distribution in distributions
    )
    widths = [
        max(len(str(row[i])) if i < len(row) else 0 for row in rows)
        for i in range(len(rows[0]))
    ]
    print(
        "\n".join(
            " ".join(
                str(value).ljust(widths[i]) for i, value in enumerate(row)
            ).rstrip()
            for row in rows
        ),
    )
    return 0
