# cpip benchmark

Local `hyperfine` benchmarks for comparing `cpip` against `uv`.

Requirements:

- `hyperfine` on `PATH`
- `uv` on `PATH`, or pass `--uv-path`

Run from this directory:

```console
uv run cpip-bench --workload offline --benchmark startup-help --benchmark lock-warm
```

The default `offline` workload is generated locally and never touches the
network. List the mirrored official uv workloads and their capabilities with:

```console
uv run cpip-bench --list-workloads
```

Run Jupyter's cold and warm resolver and installer comparisons:

```console
uv run cpip-bench --workload jupyter
```

Run every official uv workload. Resolver-only workloads run the cold and warm
lock cases; workloads with an upstream `compiled/*.txt` fixture also run cold
and warm installation cases:

```console
uv run cpip-bench --workload live
```

To limit the complete corpus to resolver benchmarks:

```console
uv run cpip-bench \
  --workload live \
  --benchmark lock-cold \
  --benchmark lock-warm
```

By default, cpip is measured as `python -m cpip`. To measure the direct
console-script style launcher, pass `--cpip-launcher direct`:

```console
uv run cpip-bench --cpip-launcher direct --benchmark startup-help
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

`startup-fast-lock`/`startup-fast-install` measure a single dependency-free
package against an already-warm cache, isolating per-invocation overhead
(process start, arg parsing, provider setup) from the graph-resolution cost
that `lock-warm`/`install-warm` measure against the full offline workload.

To run the Trio/PyPI workload used by uv's public benchmark documentation:

```console
uv run cpip-bench --workload trio --benchmark lock-cold --benchmark install-cold
```

`live` is the suite selector for the complete official corpus; concrete names
such as `trio` select one workload. The corpus is mirrored in
[`requirements`](requirements/README.md), including source inputs, compiled
installer inputs, Airflow constraints, explicit backtracking cases, and the
Transformers project fixture.

Official uv workloads are intentionally opt-in because they can depend on
network latency, current PyPI state, VCS availability, target Python, platform
wheels, and cache behavior outside this repository. `--list-workloads` reports
cases with an upstream recommended Python version.

Two benchmark modes from uv's own harness
(`astral-sh/uv/scripts/benchmark/src/benchmark/resolver.py`'s `Benchmark`
enum) are deliberately not in `BENCHMARKS` above: `resolve-incremental` (add
one new dependency to an existing lockfile, re-lock) and `resolve-noop`
(re-lock against a lockfile that already satisfies the input, expecting a
cheap confirmation). Both measure whether a tool reuses an existing lockfile
instead of fully re-resolving. `cpip lock` has no such reuse path -- it
always resolves from scratch regardless of what's already on disk at
`--output` -- so running either case against cpip would just be `lock-warm`
again under a different name, not a distinct measurement. Revisit if `cpip
lock` ever grows preferred-versions-from-an-existing-lockfile support.

## Comparing two runs

`--json` also writes a `meta.json` recording the interpreter/uv versions and
git commit used, alongside the per-benchmark `--export-json` files. To
compare a change against a baseline, run `--json` once per checkout into
separate directories, then:

```console
uv run cpip-bench-compare before/ after/
```

This prints a before/after/delta table per benchmark and tool, and warns if
`meta.json` shows the two runs used different interpreters -- a fresh `uv
sync` with no Python pin can silently resolve a different version than an
existing checkout, which will otherwise look like a real performance change.
