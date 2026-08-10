"""Implementation of the ``cpip hash`` subcommand.

Split out of ``cli/inspect.py`` so that digesting a file never pays for the
metadata stack that ``check``/``show``/``inspect`` need -- ``hash`` is the only
command among the four that does not touch installed-distribution metadata at
all.
"""

from __future__ import annotations


def run_hash(args: list[str]) -> int:
    import hashlib
    import os

    from cpip.cli.parsers.inspect import create_hash_parser

    options = create_hash_parser().parse_args(args)
    for filename in options.files:
        digest = hashlib.new(options.algorithm)
        with open(filename, "rb") as file:
            while True:
                block = file.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        print(
            f"{os.path.basename(filename)}: --hash={options.algorithm}:{digest.hexdigest()}",
        )
    return 0
