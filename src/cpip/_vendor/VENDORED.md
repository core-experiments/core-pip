# Vendored dependencies

This directory contains the vendored runtime dependency set: the HTTP
transport stack plus the dependency resolver and its typing shim. Versions
are intentionally pinned so cpip remains usable without packages installed
in the host environment.

| Package | Version | License |
| --- | --- | --- |
| requests | 2.32.4 | Apache-2.0 |
| urllib3 | 2.6.3 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| charset-normalizer | 3.4.9 | MIT |
| idna | 3.18 | BSD-3-Clause |
| nab-resolver | 0.0.13.dev0 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |

The corresponding license texts are under `licenses/`. To refresh this stack,
resolve each pinned release above for Python 3.9, copy the package sources
and license texts here, remove generated caches and native optional modules,
then update this file and run the full test suite.

## Local patches

A refresh overwrites these. Re-apply them, or land them upstream first and
drop the entry once the pinned version carries the change.

| Package | Patch | Why |
| --- | --- | --- |
| nab-resolver | `ranges.py`: `is_subset`, `is_disjoint`, `relation`, `__contains__`, `__sub__`, and `Range.__hash__` | `is_subset` and `is_disjoint` built a whole complement and intersection only to ask whether the result was empty, and `relation` called them up to three times. They now walk the interval lists once and stop early, `__contains__` binary-searches the sorted intervals instead of scanning them, which matters because a decision tests every release of a package against the same range, `__sub__` carves intervals directly instead of building the complement of its operand and intersecting, and a range hashes its intervals once instead of on every cache lookup. On a 64-release backtracking workload this removes 85% of `Range.__and__` calls and about 40% of resolution time. Behavior is unchanged: `tests/resolution/test_ranges.py` differential-tests the walks against the set-algebra definitions they replace. |
| nab-resolver | `partial_solution.py`: `backtrack` rebuilds from per-assignment `cum_positive`/`cum_negative`/`cum_decision` snapshots | `backtrack` walked every package in the trail index and rescanned each one's surviving assignments to recover its positive range, negative range and decision. Each `Assignment` now carries the package's state as of that entry, so backtracking visits only the packages it popped and reads the surviving top entry outright. Behavior is unchanged: `tests/resolution/test_partial_solution.py` compares the incrementally maintained state against a replay of the surviving assignments over randomized decide/derive/backtrack sequences. |
| nab-resolver | `decide.py`, `resolver.py`, `conflict.py`, `partial_solution.py`: sort keys cached across decision scans | `choose_package_to_decide` rebuilt every undecided package's sort key on every decision, which is quadratic over a resolution and dominates once a requirements file gets wide (27% of a 600-root resolve). Keys now persist in `Resolver.priority_keys` and are dropped only as their inputs move: `PartialSolution.drain_touched` reports ranges, `ResolverStats.drain_priority_touched` reports conflict and culprit counts (recorded in `__setitem__`, so no call site can miss one), and the new `ResolverProvider.consume_priority_invalidations` reports provider state. A provider that does not implement it, or returns `None`, gets the previous full rebuild. Behavior is unchanged: `tests/resolution/test_decision_key_cache.py` compares the whole decision sequence against the uncached path and drives each invalidation source separately. **Reusing a key whose input moved is not a slower resolution but a differently-ordered one** -- caching with no invalidation still resolves every benchmark graph correctly while taking 47% longer on the backtracking workload, so a refresh must re-apply all four reporting sites, not just `decide.py`. |
