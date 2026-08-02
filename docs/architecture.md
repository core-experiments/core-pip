# cpip architecture

This page is the map for finding code. It describes ownership and the runtime
paths without changing them. The names in the diagrams are the canonical
places to start reading.

## Start here

| Question | Start at | Then follow |
| --- | --- | --- |
| What happens when `cpip` starts? | `src/cpip/cli/entrypoint.py:main` | bootstrap, fast paths, fallback dispatch |
| Where is a command implemented? | `src/cpip/cli/commands/registry.py` | the command's `run_*` function |
| How are dependencies resolved? | `src/cpip/resolution/engine/api.py:ResolutionEngine` | `resolution/engine/runtime.py`, `resolution/engine/state/`, `CandidateProvider`, search and policy operations |
| Where do index candidates come from? | `src/cpip/index/provider.py:CandidateProvider` | source locations and link parsing |
| How are wheels installed? | `src/cpip/install/wheel_transaction.py` | archive validation, transaction, commit/rollback |
| How are packages downloaded? | `src/cpip/network/download.py` | HTTP transport and cache |
| How are build backends invoked? | `src/cpip/build/build_backend.py:ProjectBuilder` | backend hooks and fallback wheel building |

## Process entry points

There are three intentionally named `main` functions. They are not three
independent command implementations:

```text
cpip.cli.main:main       compatibility export
        |
        v
cpip.cli.entrypoint:main canonical process entry point
        |
        +--> --help / --version: dependency-light output
        +--> install fast path: cli.commands.fast_install:run
        +--> lock fast path: cli.commands.fast_lock:run
        `--> cli._fallback_main:run
                  |
                  +--> logging and execution context
                  +--> cli._main_fallback:run
                  `--> cli.commands.registry:get_command_runner
```

`cpip.__init__:main` is the package-level entry point. New process-level
behavior belongs in `cpip.cli.entrypoint:main`; new command behavior belongs
in the command module and its `CommandSpec` entry.

The command registry stores module and function names and imports command
modules only when a command is selected. Keep that lazy boundary intact: it is
part of the startup performance contract.

## Resolution and installation

The normal dependency path is:

```text
CLI command
  -> requirement collection
  -> resolution.engine.api:ResolutionEngine.resolve
       -> resolution.engine.input: requirement coercion and file models
       -> resolution.engine.runtime: provider setup and resolution orchestration
       -> resolution.engine.state: agenda, domains, plans, and requests
       -> resolution.engine.algorithms: candidate, version, hash, graph, and URL primitives
       -> resolution.engine.loop: search, selection, and backtracking
       -> resolution.engine.conflict_learning: learned incompatibilities
       -> resolution.engine.policy / validation: candidate and hash checks
       -> index.provider:CandidateProvider
            -> source_locations / page_parsing / links
       -> index.candidate_evaluators:CandidateEvaluator
            -> index.candidate_filters: hash and wheel-tag policy
       -> index.candidate_materialization:CandidateMaterializer
            -> index.candidate_stream:CandidateStream
            -> index.candidate_cache: artifact hashes and wheel cache
       -> dependency search and conflict learning
  -> install.wheel_builder / install.preparer
  -> install.wheel_transaction
```

`resolution.engine.context` contains type-only protocols for the resolver's
configuration, search state, conflict state, and operation boundaries. These
protocols document the shared resolver state without adding runtime coupling.

The public `ResolutionEngine` is composed from these implementation domains:

- `SearchLoop`: candidate selection and backtracking search.
- `ConflictLearning`: learned incompatibilities and version-domain reasoning.
- `PolicyChecks`: candidate policy, Python compatibility, and hash validation.

These operations are mixed into `ResolutionRuntime` and operate through the
type-only context protocols; they do not expose a second public resolver API.

The local pure-wheel benchmark path is separate and deliberately narrow:

```text
cli.entrypoint
  -> cli.commands.fast_install:run
  -> cli.fast_install:resolve_simple_wheelhouse
       -> resolution.engine.sources.wheelhouse: catalog, metadata, search
  -> cli.fast_install:install_resolved_pure_wheels
```

The normal install command also uses `cli.fast_install:install_resolved_pure_wheels`
as a hybrid optimization when its resolved plan is an empty-target install of
pure wheels. The installer accepts the small `PureWheelCandidate` protocol
(`canonical_name` and `path`), so it can consume both the lightweight
`FastCandidate` records and the normal resolver's wheel candidates. It validates
the wheel archive and writes directly to the target; unsupported layouts or
write failures return control to the normal transactional installer.

Do not route unsupported inputs through this path. The fast path is deliberately
narrow and must return `None` or `False` so the fallback remains the
compatibility and feature-complete path.

## Package ownership

| Package | Owns | Should not own |
| --- | --- | --- |
| `cli` | argument parsing, command dispatch, presentation, narrow fast paths | general candidate selection or normal archive transactions |
| `core` | shared data models, packaging rules, URLs, hashes, wheels | command-specific policy |
| `index` | sources, links, catalogs, candidate discovery | dependency backtracking |
| `resolution` | requirements, constraints, candidate selection, dependency search | filesystem installation |
| `install` | preparation, build environments, extraction, transactions | index page parsing |
| `network` | HTTP, authentication, downloads, progress, HTTP cache | resolver state |
| `platform` | configuration, locations, schemes, host behavior | package policy |
| `build` | build backends, metadata, build artifacts | resolver decisions |
| `vcs` | VCS URL handling and source retrieval | wheel selection |

Within `index` and `resolution`, keep the split visible:

  - `index/provider.py` owns candidate discovery and coordinates source links,
    evaluation, and materialization.
  - `index/candidate_evaluators.py` owns link validity, project/version policy,
    supported-wheel selection, and candidate ordering.
  - `index/candidate_filters.py` owns reusable hash filtering and supported-tag
    ranking primitives used by the evaluator.
  - `index/candidate_materialization.py` owns the materializer orchestration:
    accepted records become lazy or concrete wheel candidates at explicit build
    boundaries.
  - `index/candidate_stream.py` owns replayable, demand-driven candidate
    iteration. It must not perform discovery or dependency resolution.
  - `index/candidate_cache.py` owns artifact hashing, built-wheel cache lookup,
    and cache persistence. It must not decide candidate eligibility.
  - `index/source_models.py` owns the records and metadata value objects passed
    between discovery, evaluation, and materialization.

  - `resolution/engine/api.py` owns orchestration and exposes the public
    `ResolutionEngine` façade.
  - `resolution/engine/state/` owns agenda, domain, request, and plan state.
  - `resolution/engine/input/` owns requirement coercion, input models, and
    requirements-file parsing; `resolution/engine/output.py` owns result
    materialization and installation ordering.
  - `resolution/engine/algorithms.py` owns stateless candidate, version, hash, graph, and URL
    algorithms. It must not acquire resolver state or command-line policy.
  - `resolution/engine/loop.py` owns the stateful search loop, frame
    management, selection, and backtracking behavior.
  - `resolution/engine/conflict_learning.py` owns learned incompatibilities,
    conflict activity, and version-domain masks. It must not perform network
    access or installation.
  - `resolution/engine/selection.py` owns installed-distribution satisfaction,
    requirement ordering, candidate counts, and provider filtering.
  - `resolution/engine/validation.py` owns Python compatibility and
    requirement/source hash validation; `policy.py` owns candidate policy and
    resolver diagnostics.
  - `resolution/engine/sources/wheelhouse/` owns the local pure-wheel catalog,
    metadata, search, and archive handling used by the fast resolver.
  - `cli/requirements.py` owns CLI requirement collection; build-backend
    invocation belongs to `build/build_backend.py` and install preparation.

When a change appears to fit two packages, keep the policy in the higher-level
owner and put only reusable mechanics in the lower-level package. This keeps
the dependency direction legible and avoids new cross-package hubs.

## Performance boundaries

These boundaries are intentionally visible because they affect both behavior
and benchmarks:

1. `cli.entrypoint` imports only bootstrap and command-name data before it
   knows which command is needed.
2. Help and version output must not import the fallback command graph.
3. Fast install and fast lock must reject unsupported inputs by returning
   `None`, allowing the normal path to take over.
4. Catalog scanning and candidate metadata loading should remain behind the
   resolver/provider boundary, where their caches can be reused.
5. Normal installation writes must remain transactional. The pure-wheel
   shortcut must validate archive layout before writing and remove its own
   partial writes on failure; unsupported cases must return to the normal
   transaction.

For a performance change, first identify which boundary it crosses. Prefer
moving work behind an existing lazy boundary or reducing repeated work inside
one owner over introducing a new shared utility imported by every command.

## Navigation recipes

For a CLI bug, start with `entrypoint.py`, identify the selected command in
`commands/registry.py`, then follow that command's `run_*` function. For a
resolution bug, start with `ResolutionEngine.resolve`, inspect the provider
method it calls, and only then descend into source parsing or materialization.
For an install bug, start at `wheel_transaction.py` and walk backward to the
preparer/builder rather than beginning in archive helpers.

The most useful searches are:

```sh
rg -n "CommandSpec|run_[a-z_]+|ResolutionEngine|CandidateProvider" src/cpip
rg -n "install_resolved_pure_wheels|resolve_simple_wheelhouse" src tests
rg -n "load_catalog|scan_catalog|search_candidates|CandidateMaterializer" src/cpip
```
