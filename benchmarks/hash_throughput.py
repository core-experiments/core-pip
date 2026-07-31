"""Digest throughput benchmarks for install and cache integrity paths."""

from __future__ import annotations

import hashlib


class Sha256Throughput:
    params = ("1 KiB", "1 MiB", "16 MiB")
    param_names = ("payload",)
    number = 10
    repeat = 5
    rounds = 2

    def setup(self, payload: str) -> None:
        sizes = {"1 KiB": 1 << 10, "1 MiB": 1 << 20, "16 MiB": 16 << 20}
        self.data = b"x" * sizes[payload]

    def time_sha256(self, payload: str) -> None:
        digest = hashlib.sha256(self.data).digest()
        if not digest:
            raise AssertionError("SHA-256 returned an empty digest")
