# pip benchmarks

This directory contains the ASV benchmark suite for pip. The cache benchmarks
follow uv's definitions:

- **cold**: recreate the output environment and delete pip's cache before every
  measured sample;
- **warm**: recreate the output environment but preserve a cache populated
  before measurement.

Both variants use a generated local wheelhouse. This isolates resolver,
candidate materialization, wheel-build cache, and installation work from public
index and network variance. It does not attempt to flush the operating system's
filesystem cache. Installation is measured with both the pip revision under
test and the uv version pinned in `asv.conf.json`. Resolver-only timing remains
pip-specific because `uv pip` has no operation equivalent to pip's
`--dry-run --report`. The uv command enables bytecode compilation to match
pip's installation work.

Run the benchmarks from the repository root:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run --quick
uv run --group benchmark asv --config benchmarks/asv.conf.json run
```

Compare the eager resolver with lazy candidate materialization:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json continuous \
  33a02b60a a412f7f4d
```

Run only the cache benchmarks:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'cache_materialization.*'
```

Run only the direct pip versus uv installation comparison:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'cache_materialization.*Install.*'
```

## Direct pip versus uv comparisons

For paired measurements over the same workload, use the Hyperfine runner. It
validates that both tools choose the same deterministic resolution before
timing them and writes a JSON manifest containing tool, Python, platform, and
Hyperfine metadata:

```console
brew install hyperfine
uv run --group benchmark python -m benchmarks.cross_tool \
  --scenario trio \
  --benchmark resolve-cold \
  --benchmark resolve-warm \
  --benchmark install-cold \
  --benchmark install-warm \
  --output-dir benchmarks/results
```

The runner uses three warmups and ten measured runs by default, with isolated
cache and output directories for each tool. `resolve-cold` and
`resolve-warm` measure the same resolver operation with different metadata
cache state; installation modes recreate the target while either clearing or
preserving the package cache. The generated `benchmarks/results/` directory is
local output and is not committed.

The deterministic comparison suite is the primary cross-tool signal. ASV
remains the source of pip revision history and in-process microbenchmarks.
Live-PyPI benchmarks remain opt-in because network and index conditions are
useful for realism but unsuitable for a stable comparison gate.

Run the cold conflict-resolution scaling benchmark:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'resolver_conflicts.*'
```

Run the real-world fast-resolver cases:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'real_world_resolver.*'
```

These cases isolate eight failure modes: catalogs with many versions,
backtracking to an older compatible branch, unsatisfiable graphs, simple and
nested conflicting extras, `Requires-Python` rejection, large catalogs with
irrelevant projects, and no-match searches. Each case has 32, 128, and 512
versions and measures both cold and warm metadata/catalog caches.

## uv-derived workloads

The suite also ports the pip-relevant benchmarks from uv commit
`73fa89457b07`:

- PEP 440 specifier parsing for short, bounded, and exclusion-heavy ranges;
- extraction, preparation, and installation of archives with 10,000 files;
- cold, warm, incremental, and no-op resolution of Trio-, Jupyter-, Airflow-,
  and historical-backtracking-shaped dependency graphs;
- cold and warm installation of those graphs with core-pip and `uv pip`.

The deterministic resolver cases generate 3,657 metadata-only wheels totaling
about 3 MiB. They preserve the expensive graph dimensions without checking
large package artifacts into Git. Run them with:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'uv_(microbenchmarks|offline).*'
```

Run only the core-pip versus uv offline resolver comparison:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'uv_offline.OfflineResolution.*'
```

Run the deterministic resolver primitive benchmarks:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'pip_primitives.*'
```

These isolate PEP 508 requirement parsing, version parsing, project-name
normalization, wheel filename parsing, local specifier checks, and version
filtering. The requirement benchmarks report both uncached parsing and pip's
cached parsing so cache effects are not mistaken for parser improvements.

Run metadata-cache scaling benchmarks:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'metadata_cache.*'
```

These use deterministic wheelhouses containing 10, 100, 1,000, and 10,000
metadata-only wheels, measured with cold, warm, and single-file-invalidation
cache states.

Run local index and requirements-file parsing benchmarks:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'io_primitives.*'
```

These scan local wheelhouses containing 10, 100, 1,000, and 10,000 files and
parse flat, nested, and constraint-bearing requirements files at the same
scales. The index cases include both wheel-only and mixed artifact directories.

Run lockfile serialization benchmarks:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'lockfile_serialization.*'
```

These measure production TOML rendering for the regular and optimized lock
paths with 10, 100, 1,000, and 10,000 packages, excluding resolution and file
I/O.

The authentic PyPI cases use uv's `2024-08-08` upload cutoff and are opt-in so
normal revision benchmarks remain deterministic. Enable them with:

```console
PIP_BENCH_LIVE=1 uv run --group benchmark asv \
  --config benchmarks/asv.conf.json run --bench 'uv_live_pypi.*'
```

The live suite covers Jupyter, Airflow, Trio, Beam/Dill, NumPy/Numba,
NumPy/Sparse, Sentry, and Starlette/FastAPI. It also installs uv's compiled Trio
environment with cold and warm caches. Use Python 3.12 for parity with uv's
benchmark fixture and to avoid source builds caused by unavailable wheels on
newer Python versions.

Run the startup comparison matrix on one deterministic scenario:

```console
uv run --group benchmark python -m benchmarks.cross_tool \
  --scenario trio \
  --benchmark startup-version \
  --benchmark startup-version-cold \
  --benchmark startup-help \
  --benchmark startup-help-cold \
  --benchmark startup-fast-install \
  --benchmark startup-fallback-install \
  --benchmark startup-full-fallback-install \
  --output-dir benchmarks/results
```

The startup install cases warm their local cache before measurement and recreate
only the target directory between samples. The fallback case omits `--quiet`
so it exercises the normal CLI path rather than the specialized fast installer.
Core-pip benchmark commands use a dedicated `PYTHONPYCACHEPREFIX`; the regular
startup cases measure warm cached imports, while the `*-cold` cases remove that
cache before every sample. This avoids inheriting an ambient
`PYTHONDONTWRITEBYTECODE` setting from the shell.

`startup-fallback-install` measures the safe local-wheel capability route with
normal output. `startup-full-fallback-install` adds an unsupported `--upgrade`
shape to force the complete resolver/install path for regression comparisons.

uv's universal resolver, workspace discovery, and tool-management benchmarks
are intentionally omitted because core-pip has no equivalent operation.

ASV stores environments, results, and generated HTML under `benchmarks/.asv/`.
The suite disables ASV's pre-build uninstall command because pip is also the
installer used to build the revision under test; the subsequent installation
still uses ASV's forced reinstall. Its build command also uses `--no-deps` so
ASV's build cache contains exactly the pip wheel it expects to install.
