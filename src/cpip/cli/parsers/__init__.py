"""Argument parsers, one module per command.

``cpip <command> --help`` only needs to build a parser and print it, but a
parser factory that lives beside its ``run_*`` function drags the whole
command -- resolver, index, installer -- into a process that is about to
print text and exit.  Keeping the factories here means ``CommandSpec`` can
reach a parser without importing the command.

Each module imports ``cpip.cli.parser`` and nothing else from cpip.  Anything
more expensive belongs in the command module.
"""

from __future__ import annotations
