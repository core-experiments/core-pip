# Vendored dependencies

This directory contains the following runtime dependency set for the HTTP
transport. Versions are intentionally pinned so cpip remains usable without
packages installed in the host environment.

| Package | Version | License |
| --- | --- | --- |
| requests | 2.32.4 | Apache-2.0 |
| urllib3 | 2.6.3 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| charset-normalizer | 3.4.9 | MIT |
| idna | 3.18 | BSD-3-Clause |

The corresponding license texts are under `licenses/`. To refresh this stack,
resolve the pinned requests release for Python 3.9, copy the package sources
and license texts here, remove generated caches and native optional modules,
then update this file and run the full test suite.
