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
| nab-resolver | `ranges.py`: `is_subset`, `is_disjoint`, `relation`, `__contains__`, and `Range.__hash__` | `is_subset` and `is_disjoint` built a whole complement and intersection only to ask whether the result was empty, and `relation` called them up to three times. They now walk the interval lists once and stop early, `__contains__` binary-searches the sorted intervals instead of scanning them, which matters because a decision tests every release of a package against the same range, and a range hashes its intervals once instead of on every cache lookup. On a 64-release backtracking workload this removes 85% of `Range.__and__` calls and about 40% of resolution time. Behavior is unchanged: `tests/resolution/test_ranges.py` differential-tests the walks against the set-algebra definitions they replace. |
