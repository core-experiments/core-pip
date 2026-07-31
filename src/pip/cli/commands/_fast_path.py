"""Small helpers shared by the narrow command fast paths."""

def option_value(args: list[str], index: int) -> "str | None":
    """Return a following option value, or ``None`` for an invalid option."""
    if index + 1 >= len(args):
        return None
    value = args[index + 1]
    return None if value.startswith("-") else value


def read_requirements(path: str) -> "list[str] | None":
    """Read simple requirement files without importing the full parser."""
    try:
        with open(path, encoding="utf-8") as requirement_file:
            return [
                line.strip()
                for line in requirement_file.read().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError:
        return None
