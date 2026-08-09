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
