# cpip architecture

This page is the map for finding code. It describes ownership and the runtime
paths without changing them. The names in the diagrams are the canonical
places to start reading.

## Start here

| Question | Start at | Then follow |
| --- | --- | --- |
| What happens when `cpip` starts? | `src/cpip/cli/entrypoint.py:main` | bootstrap, fast paths, fallback dispatch |
| Where is a command implemented? | `src/cpip/cli/commands/registry.py` | the command's `run_*` function |
| How are dependencies resolved? | `src/cpip/resolution/resolver.py:Resolver` | `resolution/resolver_internals/state/`, `resolution/algorithms.py`, `CandidateProvider`, candidate search |
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
  -> resolution.resolver:Resolver.resolve
       -> resolution.resolver_internals.state: agenda, domains, plans, requests
       -> resolution.algorithms: candidate, version, hash, and URL primitives
       -> resolution.resolver_internals: search, selection, conflicts, checks
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

`resolution.resolver_internals` contains the resolver’s private operation
domains and the type-only `ResolverContext` protocol. It documents shared
resolver state without adding a runtime import or compatibility layer.

The public `Resolver` is composed from three implementation domains:

- `ResolverSearch`: candidate selection and backtracking search.
- `ResolverConflicts`: learned incompatibilities and version-domain reasoning.
- `ResolverChecks`: candidate policy, compatibility, and hash validation.

The domain classes are deliberately flat operations on the resolver context;
they do not allocate helper objects during resolution.

The local pure-wheel benchmark path is separate and deliberately narrow:

```text
cli.entrypoint
  -> cli.commands.fast_install:run
  -> resolution.fast_local_wheelhouse:resolve
       -> resolution.fast_wheelhouse: catalog, metadata, search, archive
  -> install.wheel_transaction:install_resolved_pure_wheels
```

Do not route this path through the full `Resolver` or normal build/network
stack unless the input no longer satisfies the fast-path assumptions. The
fallback is the compatibility and feature-complete path; the fast path is the
performance path.

## Package ownership

| Package | Owns | Should not own |
| --- | --- | --- |
| `cli` | argument parsing, command dispatch, presentation | candidate selection or archive mutation |
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

  - `resolver.py` owns orchestration and exposes the public `Resolver` façade.
  - `resolver_internals/state/` owns rollback-friendly agenda, domain, request,
    and plan state.
  - `resolver_internals/inputs.py` owns requirement coercion and result-input
    mapping; `resolver_internals/outputs.py` owns plan materialization and
    installation ordering.
  - `algorithms.py` owns stateless candidate, version, hash, graph, and URL
    algorithms. It must not acquire resolver state or command-line policy.
  - `resolver_internals/search.py` owns the stateful search loop, frame
    management, and backtracking behavior.
  - `resolver_internals/conflicts.py` owns learned incompatibilities, conflict activity, and
    version-domain masks. It must not perform network access or installation.
  - `resolver_internals/selection.py` owns installed-distribution satisfaction, requirement
    ordering, candidate counts, and provider filtering.
  - `resolver_internals/validation.py` owns Python compatibility and requirement/source hash
    validation.
  - `fast_wheelhouse/` owns the lightweight local-wheel implementation; its
    `fast_local_wheelhouse.py` module is an explicit command-facing façade.
  - `requirements/` owns requirement-line parsing, paths, and build-backend
    invocation; `requirement_files/` owns requirements-file and pylock parsing.

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
5. Installation writes must remain transactional; performance shortcuts must
   not bypass validation or rollback.

For a performance change, first identify which boundary it crosses. Prefer
moving work behind an existing lazy boundary or reducing repeated work inside
one owner over introducing a new shared utility imported by every command.

## Navigation recipes

For a CLI bug, start with `entrypoint.py`, identify the selected command in
`commands/registry.py`, then follow that command's `run_*` function. For a
resolution bug, start with `Resolver.resolve`, inspect the provider method it
calls, and only then descend into source parsing or materialization. For an
install bug, start at `wheel_transaction.py` and walk backward to the
preparer/builder rather than beginning in archive helpers.

The most useful searches are:

```sh
rg -n "CommandSpec|run_[a-z_]+|Resolver|CandidateProvider" src/cpip
rg -n "install_resolved_pure_wheels|fast_local_wheelhouse" src tests benchmarks
rg -n "load_catalog|scan_catalog|search_candidates|materialize" src/cpip
```
