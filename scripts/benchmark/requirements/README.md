# uv benchmark workloads

This directory mirrors the complete workload corpus from
[`astral-sh/uv/test/requirements`](https://github.com/astral-sh/uv/tree/main/test/requirements)
at upstream commit `79bbface771210df216b738e9bdc7df95e5a9e6b`.

The files are kept in their upstream layout so resolver inputs, constraint
files, compiled installer inputs, backtracking cases, and project fixtures can
be compared directly between cpip and uv. The `live` selector runs this complete
corpus; `trio` selects only the Trio workload.

uv is distributed under the Apache-2.0 and MIT licenses. See the upstream
[`LICENSE-APACHE`](https://github.com/astral-sh/uv/blob/main/LICENSE-APACHE) and
[`LICENSE-MIT`](https://github.com/astral-sh/uv/blob/main/LICENSE-MIT) files.
