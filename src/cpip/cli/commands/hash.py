"""Implementation of the ``cpip hash`` command."""

from __future__ import annotations

import hashlib
import os

from cpip.cli.parser import ArgumentParser


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="cpip hash")
    parser.add_argument("files", nargs="+")
    parser.add_argument(
        "-a",
        "--algorithm",
        default="sha256",
        choices=sorted(hashlib.algorithms_available),
    )
    return parser


def run_hash(args: list[str]) -> int:
    options = create_parser().parse_args(args)
    for filename in options.files:
        digest = hashlib.new(options.algorithm)
        with open(filename, "rb") as file:
            while True:
                block = file.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        print(
            f"{os.path.basename(filename)}: "
            f"--hash={options.algorithm}:{digest.hexdigest()}",
        )
    return 0
