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

Run the cold conflict-resolution scaling benchmark:

```console
uv run --group benchmark asv --config benchmarks/asv.conf.json run \
  --bench 'resolver_conflicts.*'
```

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

uv's universal resolver, workspace discovery, and tool-management benchmarks
are intentionally omitted because core-pip has no equivalent operation.

ASV stores environments, results, and generated HTML under `benchmarks/.asv/`.
The suite disables ASV's pre-build uninstall command because pip is also the
installer used to build the revision under test; the subsequent installation
still uses ASV's forced reinstall. Its build command also uses `--no-deps` so
ASV's build cache contains exactly the pip wheel it expects to install.
