"""Small local-wheel lock command entrypoint."""

import marshal
import os
import sys

from cpip.cli.fast_path import consume_option, extend_requirements
from cpip.cli.lock_format import render_wheel_lock


class LockOptions:
    __slots__ = ("find_links", "no_index", "output", "requirements")

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


def parse_arguments(args: list[str]) -> "LockOptions | None":
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

        option = consume_option(
            args,
            index,
            ("-f", "--find-links", "-r", "--requirement", "--output"),
        )
        if option is not None:
            name, value, index = option
            if name in ("-f", "--find-links"):
                find_links.append(value)
            elif name in ("-r", "--requirement"):
                if not extend_requirements(
                    requirements,
                    value,
                    reject_pylock=True,
                ):
                    return None

            else:
                output = value
            continue

        if token.startswith("-"):
            return None

        else:
            requirements.append(token)

        index += 1

    return LockOptions(requirements, find_links, no_index, output)


def render_lock(packages: list[tuple[str, str, str, str, str]]) -> str:
    return render_wheel_lock(packages)

def cache_digest(value: bytes) -> str:
    digest = 14695981039346656037

    for byte in value:
        digest = (digest ^ byte) * 1099511628211 & 0xFFFFFFFFFFFFFFFF

    return f"{digest:016x}"


def plan_cache_key(options: LockOptions) -> "PlanCacheKey | None":
    signatures: list[tuple[str, str, int, int]] = []

    for value in options.find_links:
        root = os.path.abspath(value)

        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.name.endswith(".whl") or not entry.is_file():
                        continue

                    stat = entry.stat()

                    signatures.append(
                        (root, entry.name, stat.st_mtime_ns, stat.st_size),
                    )

        except NotADirectoryError:
            if not value.endswith(".whl"):
                continue

            try:
                stat = os.stat(value)

            except OSError:
                return None

            signatures.append(
                (os.path.abspath(value), "", stat.st_mtime_ns, stat.st_size),
            )

            continue

        except OSError:
            return None

    return (
        sys.version_info[:3],
        sys.platform,
        tuple(options.requirements),
        tuple(options.find_links),
        tuple(sorted(signatures)),
    )


def cache_path(options: LockOptions) -> "str | None":
    root = os.environ.get("CPIP_CACHE_DIR")

    if not root:
        return None

    key = (
        sys.version_info[:3],
        sys.platform,
        tuple(options.requirements),
        tuple(options.find_links),
    )

    try:
        serialized = marshal.dumps(key)

        digest = cache_digest(serialized)

    except (OSError, TypeError, ValueError):
        return None

    return os.path.join(root, "fast-lock-plan-v2", f"{digest}.cache")


def load_plan_cache(path: "str | None", key: "bytes | None") -> "str | None":
    if path is None or key is None:
        return None

    try:
        with open(path, "rb") as file:
            size = int.from_bytes(file.read(8), "big")

            if file.read(size) != key:
                return None

            return file.read().decode("utf-8")

    except (OSError, UnicodeDecodeError, ValueError):
        return None


def save_plan_cache(path: "str | None", key: "bytes | None", rendered: str) -> None:
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


def write_output(output: str, rendered: str) -> None:
    if output == "-":
        print(rendered, end="")

    else:
        with open(output, "w", encoding="utf-8") as output_file:
            output_file.write(rendered)


def run(args: list[str]) -> "int | None":
    """Run the local-wheel fast path, or return ``None`` for fallback."""

    options = parse_arguments(args)

    if options is None or not options.no_index or not options.requirements:
        return None

    cache_file = cache_path(options)

    plan_key = plan_cache_key(options)

    if plan_key is None:
        return None

    serialized_plan_key = marshal.dumps(plan_key)

    cached_output = load_plan_cache(cache_file, serialized_plan_key)

    if cached_output is not None:
        write_output(options.output, cached_output)

        return 0

    from cpip.resolution.api import ResolutionEngine

    plan = ResolutionEngine.resolve_wheelhouse(options.find_links, options.requirements)


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
            ),
        )

    rendered = render_lock(packages)

    save_plan_cache(cache_file, serialized_plan_key, rendered)

    write_output(options.output, rendered)

    return 0
