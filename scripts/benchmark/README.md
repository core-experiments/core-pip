# cpip benchmark

Local `hyperfine` benchmarks for comparing `cpip` against `uv`.

Requirements:

- `hyperfine` on `PATH`
- `uv` on `PATH`, or pass `--uv-path`

Run from this directory:

```console
uv run cpip-bench --workload offline --benchmark startup-help --benchmark lock-warm
```

Startup-focused cases:

```console
uv run cpip-bench \
  --benchmark startup-help \
  --benchmark startup-version \
  --benchmark startup-install-help \
  --benchmark startup-lock-help \
  --benchmark startup-list-help \
  --benchmark startup-invalid-command \
  --benchmark startup-list-empty \
  --benchmark startup-fast-lock \
  --benchmark startup-fast-install
```

The default workload is generated locally and does not touch the network. To run
the Trio/PyPI workload used by uv's public benchmark documentation:

```console
uv run cpip-bench --workload live --benchmark lock-cold --benchmark install-cold
```

Live benchmarks are intentionally opt-in because they depend on network latency,
PyPI state, and cache behavior outside this repository.
