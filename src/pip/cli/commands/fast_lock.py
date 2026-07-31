"""Small local-wheel lock command entrypoint."""

import os
import sys


class _Options:
    __slots__ = ("requirements", "find_links", "no_index", "output")

    def __init__(
        self,
        requirements: list[str],
        find_links: list[str],
        no_index: bool,
        output: str,
    ) -> None:
        self.requirements = requirements
        self.find_links = find_links
        self.no_index = no_index
        self.output = output


PlanCacheKey = tuple[object, ...]


def _parse(args: list[str]) -> "_Options | None":
    from pip.cli.commands._fast_path import option_value, read_requirements

    requirements: list[str] = []
    find_links: list[str] = []
    no_index = False
    output = "pylock.toml"
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--no-index":
            no_index = True
            index += 1
            continue
        if token == "--quiet":
            index += 1
            continue
        if token in ("-f", "--find-links", "-r", "--requirement", "--output"):
            value = option_value(args, index)
            if value is None:
                return None
            if token in ("-f", "--find-links"):
                find_links.append(value)
            elif token in ("-r", "--requirement"):
                if os.path.basename(value).startswith("pylock") and value.endswith(
                    ".toml"
                ):
                    return None
                values = read_requirements(value)
                if values is None:
                    return None
                requirements.extend(values)
            else:
                output = value
            index += 2
            continue
        if token.startswith("--find-links="):
            find_links.append(token.partition("=")[2])
        elif token.startswith("--requirement="):
            value = token.partition("=")[2]
            if os.path.basename(value).startswith("pylock") and value.endswith(".toml"):
                return None
            values = read_requirements(value)
            if values is None:
                return None
            requirements.extend(values)
        elif token.startswith("--output="):
            output = token.partition("=")[2]
        elif token.startswith("-"):
            return None
        else:
            requirements.append(token)
        index += 1
    return _Options(requirements, find_links, no_index, output)


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_lock(packages: list[tuple[str, str, str, str, str]]) -> str:
    lines = ['created-by = "pip"', 'lock-version = "1.0"', ""]
    for name, version, wheel_name, wheel_url, digest in packages:
        lines.extend(
            (
                "[[packages]]",
                f"name = {_toml_string(name)}",
                f"version = {_toml_string(version)}",
                "[[packages.wheels]]",
                f"name = {_toml_string(wheel_name)}",
                f"url = {_toml_string(wheel_url)}",
                "[packages.wheels.hashes]",
                f"sha256 = {_toml_string(digest)}",
                "",
            )
        )
    return "\n".join(lines)


def _cache_digest(value: bytes) -> str:
    digest = 14695981039346656037
    for byte in value:
        digest = (digest ^ byte) * 1099511628211 & 0xFFFFFFFFFFFFFFFF
    return f"{digest:016x}"


def _plan_cache_key(options: _Options) -> "PlanCacheKey | None":
    signatures: list[tuple[str, str, int, int]] = []
    for value in options.find_links:
        if os.path.isdir(value):
            root = os.path.abspath(value)
            try:
                with os.scandir(root) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".whl") or not entry.is_file():
                            continue
                        stat = entry.stat()
                        signatures.append(
                            (root, entry.name, stat.st_mtime_ns, stat.st_size)
                        )
            except OSError:
                return None
        elif os.path.isfile(value):
            if not value.endswith(".whl"):
                continue
            try:
                stat = os.stat(value)
            except OSError:
                return None
            signatures.append(
                (os.path.abspath(value), "", stat.st_mtime_ns, stat.st_size)
            )
            continue
        else:
            return None
    return (
        sys.version_info[:3],
        sys.platform,
        tuple(options.requirements),
        tuple(options.find_links),
        tuple(sorted(signatures)),
    )


def _cache_path(options: _Options) -> "str | None":
    root = os.environ.get("PIP_CACHE_DIR")
    if not root:
        return None
    key = (
        sys.version_info[:3],
        sys.platform,
        tuple(options.requirements),
        tuple(options.find_links),
    )
    try:
        import marshal

        serialized = marshal.dumps(key)
        digest = _cache_digest(serialized)
    except (OSError, TypeError, ValueError):
        return None
    return os.path.join(root, "fast-lock-plan-v2", f"{digest}.cache")


def _load_plan_cache(path: "str | None", key: "bytes | None") -> "str | None":
    if path is None or key is None:
        return
    try:
        with open(path, "rb") as file:
            size = int.from_bytes(file.read(8), "big")
            if file.read(size) != key:
                return None
            return file.read().decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _save_plan_cache(path: "str | None", key: "bytes | None", rendered: str) -> None:
    if path is None or key is None:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.{os.getpid()}.tmp"
        with open(temporary, "wb") as file:
            file.write(len(key).to_bytes(8, "big"))
            file.write(key)
            file.write(rendered.encode("utf-8"))
        os.replace(temporary, path)
    except OSError:
        pass


def _write_output(output: str, rendered: str) -> None:
    if output == "-":
        print(rendered, end="")
    else:
        with open(output, "w", encoding="utf-8") as output_file:
            output_file.write(rendered)


def run(args: list[str]) -> "int | None":
    """Run the local-wheel fast path, or return ``None`` for fallback."""
    options = _parse(args)
    if options is None or not options.no_index or not options.requirements:
        return None
    cache_path = _cache_path(options)
    plan_key = _plan_cache_key(options)
    if plan_key is None:
        return None
    import marshal

    serialized_plan_key = marshal.dumps(plan_key)
    cached_output = _load_plan_cache(cache_path, serialized_plan_key)
    if cached_output is not None:
        _write_output(options.output, cached_output)
        return 0
    from pip.resolution.fast_local_wheelhouse import resolve as resolve_local_wheelhouse

    plan = resolve_local_wheelhouse(options.find_links, options.requirements)
    if plan is None:
        return None
    packages: list[tuple[str, str, str, str, str]] = []
    for candidate in plan.candidates:
        source = candidate.source_url
        if source is None:
            return None
        digest = (candidate.source_hashes or {}).get("sha256")
        if digest is None:
            import hashlib

            with open(candidate.path, "rb") as wheel_file:
                digest = hashlib.sha256(wheel_file.read()).hexdigest()
        packages.append(
            (
                candidate.name,
                str(candidate.version),
                os.path.basename(candidate.path),
                source,
                digest,
            )
        )
    rendered = _render_lock(packages)
    _save_plan_cache(cache_path, serialized_plan_key, rendered)
    _write_output(options.output, rendered)
    return 0
