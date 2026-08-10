"""Compare two directories of ``cpip-bench --json`` exports.

Usage::

    cpip-bench --json --workload offline   # run once per checkout, in
                                            # separate directories
    cpip-bench-compare before/ after/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

Command = str
BenchmarkName = str
MeanStddevMs = tuple[float, float]


def load_run(directory: Path) -> dict[BenchmarkName, dict[Command, MeanStddevMs]]:
    run: dict[BenchmarkName, dict[Command, MeanStddevMs]] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name == "meta.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        run[path.stem] = {
            result["command"]: (result["mean"] * 1000, result["stddev"] * 1000)
            for result in data["results"]
        }
    return run


def load_metadata(directory: Path) -> dict[str, str] | None:
    meta_path = directory / "meta.json"
    if not meta_path.is_file():
        return None
    return json.loads(meta_path.read_text(encoding="utf-8"))


def tool_label(command: str) -> str:
    return command.split(" ", 1)[0]


def warn_on_mismatched_metadata(
    before: dict[str, str] | None, after: dict[str, str] | None
) -> None:
    if before is None or after is None:
        print(
            "Warning: at least one directory has no meta.json (run with "
            "--json to record it). Interpreter/uv version differences "
            "between the two runs cannot be checked.",
            file=sys.stderr,
        )
        return
    for key in ("cpip_python_version", "uv_version"):
        if before[key] != after[key]:
            print(
                f"Warning: {key} differs between runs "
                f"({before[key]!r} vs {after[key]!r}). Deltas below may "
                "reflect a different interpreter/tool, not a real change.",
                file=sys.stderr,
            )


def compare(before: Path, after: Path) -> int:
    before_runs = load_run(before)
    after_runs = load_run(after)
    names = sorted(set(before_runs) & set(after_runs))
    missing = sorted(set(before_runs) ^ set(after_runs))
    if not names:
        print(
            "No matching benchmark names between the two directories.", file=sys.stderr
        )
        return 1

    warn_on_mismatched_metadata(load_metadata(before), load_metadata(after))

    header = f"{'benchmark':<28}{'tool':<6}{'before':>12}{'after':>12}{'delta':>10}"
    print(header)
    print("-" * len(header))
    for name in names:
        for command, (before_mean, _before_stddev) in sorted(before_runs[name].items()):
            after_value = after_runs[name].get(command)
            if after_value is None:
                continue
            after_mean, _after_stddev = after_value
            delta_pct = (after_mean - before_mean) / before_mean * 100
            print(
                f"{name:<28}{tool_label(command):<6}"
                f"{before_mean:>9.1f}ms{after_mean:>9.1f}ms{delta_pct:>+9.1f}%",
            )

    if missing:
        print()
        print(f"Skipped (present in only one directory): {', '.join(missing)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    for label, directory in (("before", args.before), ("after", args.after)):
        if not directory.is_dir():
            parser.error(f"{label} directory not found: {directory}")
    raise SystemExit(compare(args.before, args.after))


if __name__ == "__main__":
    main()
