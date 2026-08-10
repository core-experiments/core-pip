# Benchmark corpora

The default suite uses `corpus/pypi_snapshot.json`, a checked-in metadata
snapshot captured from PyPI on 2026-07-31. It is offline and reproducible.

`corpus/uv_workloads/` holds compiled lockfiles copied from uv's own
benchmark corpus (Airflow, Black, Boto3, Jupyter, Trio, and others) — see
`corpus/uv_workloads/README.md` for provenance. They're pinned requirement
sets, so `test_benchmark_uv_corpus.py` can parse them offline with no
resolution or network access required.

The live PyPI benchmarks are intentionally skipped by default. Enable them
explicitly when network variability is acceptable:

```console
CPIP_RUN_LIVE_BENCHMARKS=1 uv run --all-groups pytest tests/benchmarks/test_benchmark_live_index.py -q
```

They cover cold index requests, warm HTTP-cache reads, a missing-project
failure path, and a live wheel `HEAD` request.
