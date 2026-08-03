# cpip architecture

This page is the map for finding code and preserving architectural boundaries.
It describes the current runtime paths, their owners, and the invariants that
performance work must retain.

The source of truth for allowed package dependencies is
`[tool.cpip.architecture]` in `pyproject.toml`. The source of truth for runtime
behavior is the implementation named in each section. The architecture tests
in `tests/test_workspace_boundaries.py` enforce the declared runtime import
graph and reject cycles.

## Start here

| Question | Start at | Then follow |
| --- | --- | --- |
| What happens when `cpip` starts? | `src/cpip/cli/entrypoint.py:main` | bootstrap output, narrow fast paths, fallback dispatch |
| Where is a command implemented? | `src/cpip/cli/commands/registry.py` | its lazy `CommandSpec` and `run_*` function |
| How does install choose a plan? | `src/cpip/cli/commands/install.py:run_install` | `install_plan.py`, cached plans, wheelhouse plans, `ResolutionEngine` |
| How are dependencies resolved? | `src/cpip/resolution/engine/api.py:ResolutionEngine` | `runtime.py`, `propagation.py`, then `loop.py` |
| Where do index candidates come from? | `src/cpip/index/provider.py:CandidateProvider` | sources, link evaluation, lazy materialization |
| How does an artifact become local? | `src/cpip/index/artifacts.py:ArtifactLocator` | artifact cache, HTTP cache, network session |
| How are selected candidates prepared? | `src/cpip/resolution/engine/output.py:prepare_install_candidates` | candidate materialization and wheel archive preparation |
| How are wheels installed? | `src/cpip/install/wheel_transaction.py:install_wheels_transactionally` | archive cache, direct transaction, staged transaction |
| Where are persistent caches defined? | the cache owner listed below | `core/marshal_cache.py` for small snapshots and `cli/cache.py` for command-facing management |
| How are build backends invoked? | `src/cpip/build/build_backend.py:ProjectBuilder` | backend hooks and `build/build.py:build_wheel_from_source` |

## Process entry and command dispatch

The console script points directly at `cpip.cli.entrypoint:main`. Both
`cpip.__init__:main` and `python -m cpip` forward to that same process entry
point. `cpip.cli.main:main` is a compatibility wrapper: it handles a small
help/version surface and otherwise imports and forwards to the canonical
entrypoint.

```text
console script / cpip.__init__:main / python -m cpip
  -> cli.entrypoint:main
       +--> dependency-light help, version, and command help
       +--> cli.commands.fast_lock:run
       +--> cli.fast_install:run_cached_remote
       +--> cli.fast_install:run_local_fallback
       +--> cli.fast_install:run
       +--> cli.fast_list:run
       `--> cli._fallback_main:run
              +--> configure execution context and logging when required
              +--> create a global temporary-directory context when required
              +--> retry the general install fast path when eligible
              `--> cli._main_fallback:run
                     +--> fast lock, then normal lock if it declines
                     `--> cli.commands.registry:get_command_runner
```

The entrypoint parses only the global options needed to choose a route. Fast
paths are conservative recognizers, not separate command semantics. They
return `None` when an argument, target state, source shape, or feature is not
supported. Fallback dispatch must remain available after every declined fast
path.

The command registry stores module and function names instead of imported
callables. It imports a command module only after that command is selected.
`CommandSpec.needs_logging` and `CommandSpec.needs_tempdir` keep unnecessary
startup work out of lightweight commands. New process-level behavior belongs
in `cli.entrypoint`; new command behavior belongs in its command module and
`CommandSpec`.

The install startup routes cover three different target states:

- `run_cached_remote` accepts a missing explicit target and a conservative
  exact-pin command. It loads a fresh plan receipt and its digest-validated
  wheel archives, then installs without initializing the normal CLI or
  resolver.
- `run` accepts the supported no-index local-wheel shape and an empty target.
  It can clone a cached completed target or run the minimal resolver and pure
  wheel installer.
- `run_local_fallback` accepts a supported non-empty local target. It reuses
  the minimal resolver but delegates replacement to the archive or normal
  transactional installer.

`fast_list` and `fast_lock` follow the same recognition rule: handle only the
declared subset and return control to normal command dispatch otherwise.

## Installation planning

The ordinary non-editable dependency batch follows three planning lanes in
priority order:

```text
cli.commands.install:run_install
  -> cli.requirements: collect roots, constraints, sources, and policy
  -> plan selection
       +--> exact remote plan receipt -> CachedInstallPlan
       +--> local pure-wheel adapter  -> InstallPlan
       `--> ResolutionEngine.resolve -> ResolutionResult
  -> deduplicate selected candidates
  -> resolution.engine.output:prepare_install_candidates
       +--> materialize lazy winning candidates
       `--> prepare immutable wheel archives when caching is enabled
  -> execute the wheel batch
       +--> narrow empty-target pure-wheel hybrid, when eligible
       `--> install.wheel_transaction:install_wheels_transactionally
  -> save an exact-plan receipt after a successful fresh installation
  -> reporting and post-install checks
```

The lanes deliberately use different internal representations:

- `ResolutionResult` is the frozen public result returned by the canonical
  engine. It contains candidate artifacts, a read-only graph, conflicts,
  already-satisfied requirements, normalized candidate views, and metrics.
- `InstallPlan` is the mutable internal resolver/local-adapter plan used while
  assembling an installation.
- `CachedInstallPlan` is a mutable, archive-backed exact-plan receipt loaded by
  the warm path.

`install.py` is the adapter between these shapes. Do not make command code or
installers depend on resolver internals beyond the shared candidate attributes
they need. A new planning lane must either return the public `ResolutionResult`
or be normalized at this boundary.

Editable requirements are built and installed through their dedicated path
before the ordinary batch. Source candidates selected by the normal resolver
are converted to wheels during candidate materialization, not by
`install/preparer.py`.

## Resolution engine

`ResolutionEngine` is the public configuration and result boundary. It keeps
the public API small and delegates execution to `ResolutionRuntime`.

```text
ResolutionEngine.resolve
  -> ResolutionRuntime.resolve_plan
       -> coerce and validate root requirements
       -> reset per-resolution state and ReleaseFrontier
       -> propagation.try_resolve
            +--> guarded local-wheelhouse kernel
            `--> guarded finite-domain kernel
       -> SearchLoop.search_internal when no kernel accepts the input
            +--> SelectionOperations
            +--> CandidateProvider
            +--> ConflictLearning
            `--> PolicyChecks and ValidationOperations
       -> installation ordering and InstallPlan assembly
       -> collect resolver, frontier, and materializer metrics
  -> ResolutionResult.from_plan
```

The specialized propagation kernels are semantic optimizations. Their
eligibility checks reject unsupported provider instrumentation, source and URL
requirements, constraints, hash modes, marker forms, installed-state shapes,
and workload shapes. A kernel miss or unsupported case must enter the generic
search loop without changing behavior.

`ResolutionRuntime` composes three implementation domains:

- `SearchLoop` owns the authoritative stateful search and backtracking loop.
- `ConflictLearning` owns learned incompatibilities, watches, activity, and
  version-domain reasoning.
- `PolicyChecks` combines candidate policy, Python compatibility, requirement
  validation, and hash validation.

`resolution/engine/context.py` contains type-only protocols for configuration,
engine state, search state, and operation boundaries. They describe shared
state without creating another runtime resolver API.

### Resolver module ownership

- `api.py` owns `ResolutionConfig`, the public `ResolutionEngine` façade, and
  conversion to `ResolutionResult`; it does not own the search state machine.
- `runtime.py` owns one invocation's orchestration, mutable caches, selected
  state, graph assembly, and metrics collection.
- `model.py` owns source-independent public result values.
- `metrics.py` owns the metrics schema exported on `ResolutionResult`.
- `state/` owns agendas, domains, requests, plans, and requirement sets.
- `input/` owns requirement coercion, input contracts and models, and
  requirements-file parsing.
- `propagation.py` owns guarded resolution kernels. It must decline any shape
  for which it cannot preserve canonical resolver semantics.
- `frontier.py` owns per-resolution release catalogs, compact version domains,
  masks, and frontier metrics. It is not persistent storage.
- `loop.py` owns generic search, frames, selection, and backtracking.
- `selection.py` owns installed-distribution satisfaction, requirement
  ordering, candidate counts, and provider filtering.
- `conflict_learning.py` owns learned incompatibilities and conflict activity;
  it must not perform network access or installation.
- `policy.py` and `validation.py` own candidate policy, diagnostics, Python
  compatibility, and requirement/source hash validation.
- `algorithms.py` owns stateless candidate, version, hash, graph, and URL
  primitives. It must not acquire resolver state or command-line policy.
- `output.py` owns source-hash finalization, winner materialization,
  installation ordering, and pipelined wheel archive preparation.

## Local wheelhouse resolution

There are two intentionally separate local-wheel implementations. Do not draw
an implementation arrow between them.

The process-level fast installer uses a minimal resolver:

```text
cli.fast_install:run
  -> cli.fast_install:resolve_simple_wheelhouse
       +--> scan wheel filenames
       +--> read selected wheel metadata through WheelArchive
       +--> recursively satisfy the supported requirement subset
       `--> reuse FastInstallMetadataCache plans and metadata
  -> cli.fast_install:install_resolved_pure_wheels
  -> cache a cloneable install tree after success
```

On a warm local install, the same path can clone a previously validated install
tree without resolving or extracting the wheels again.

The canonical engine uses the richer wheelhouse source:

```text
ResolutionEngine.resolve_wheelhouse
  -> resolution.engine.sources.wheelhouse.engine:resolve
       -> catalog, metadata, compatibility checks, and search
       -> ResolutionResult
```

The canonical wheelhouse engine is also used by the normal install plan adapter
and by the resolver's guarded local-wheelhouse propagation kernel. It supports
the canonical candidate/result boundary; the process-level resolver exists to
keep startup and object construction out of a deliberately narrow command
shape.

`install_resolved_pure_wheels` accepts only the small candidate shape needed by
the shortcut (`canonical_name` and `path`). That shape is currently expressed
by `cli.fast_install.PureWheelCandidate`; it is a compatibility seam, not a
domain owner. New shared candidate abstractions belong in `core` or `install`,
not in `cli`.

## Index discovery and candidate materialization

The generic candidate pipeline is demand-driven:

```text
CandidateProvider.find_candidates
  -> source locations and Simple API / find-links catalogs
  -> Link records
  -> CandidateEvaluator and candidate_filters
  -> accepted CandidateRecord values
  -> CandidateMaterializer.iter_materialize
  -> CandidateStream[WheelCandidate]
```

`CandidateProvider` coordinates sources, catalog acquisition, link evaluation,
ordering, prefetch, and process-local discovery caches. It owns neither
dependency backtracking nor filesystem installation.

`CandidateEvaluator` owns link validity, project/version policy, wheel-tag
compatibility, yanked/release policy, and candidate ordering.
`candidate_filters.py` owns reusable hash and supported-tag filtering
primitives. `source_models.py` owns the records passed between discovery,
evaluation, and materialization.

`CandidateMaterializer` turns accepted records into lazy or concrete
`WheelCandidate` objects. It localizes artifacts only when metadata or the
selected winner requires them. Source trees and sdists are built through
`build.build:build_wheel_from_source`, which reaches build backends through
`ProjectBuilder`.

`CandidateStream` is a replayable sequence that advances its source on demand.
Boolean checks and indexed access may materialize only enough candidates to
answer the operation; full length or unrestricted slicing exhausts the stream.
It must not perform discovery or dependency resolution itself.

## Artifact acquisition

`index.artifacts:ArtifactLocator` is the canonical package-artifact boundary
for resolution, download, and materialization. It handles local paths, file
URLs, cached HTTP bodies, content-addressed artifacts, and network streaming.

```text
CandidateMaterializer / download command
  -> ArtifactLocator.ensure_local[_text]
       +--> existing local path
       +--> ArtifactCache URL receipt or expected SHA-256 body
       +--> SafeFileCache HTTP body
       `--> NetworkSession streaming request
  -> local artifact path plus observed hashes
```

`network/download.py:Downloader` remains the resumable/progress-oriented
download implementation used by the preparer/bootstrap path. It is not the
primary artifact-localization route for the current resolver.

## Wheel installation

The normal batch installer is a dispatcher with three implementations:

```text
install_wheels_transactionally
  +--> install_wheels_from_archive_cache
  |      clone unpacked immutable wheels into a staged target
  |      relocate `.data`, rewrite metadata, then atomically swap the target
  +--> install_wheels_directly
  |      preflight destinations, install with per-wheel transactions,
  |      then adopt them into one batch transaction
  `--> generic staged WheelInstaller path
         stage wheels, validate destinations, commit as one InstallTransaction
```

The archive path is eligible for self-contained targets, wheel candidates, an
enabled cache, disabled bytecode compilation, and compatible install options.
Existing targets are cloned into a stage; changed distributions are removed
there; cached trees are cloned in; and the completed stage replaces the target.
If the final rename fails, the previous target is restored.

The direct path is used only after a full destination preflight proves that the
batch can be installed safely without replacement conflicts. The generic path
handles the remaining layouts and installed-state cases. Every successful
normal route preserves batch rollback semantics.

The CLI pure-wheel hybrid is narrower than these normal routes. It requires an
empty explicit target and validates archive paths, purelib layout, duplicate
destinations, and unsupported wheel features before writing. It cleans partial
writes and returns `False` when the normal transaction must take over.

`install/preparer.py` and `install/wheel_builder.py` support build-environment
and compatibility/bootstrap flows. They are not the starting point for an
ordinary resolved install.

## Cache architecture

cpip currently uses independent filesystem caches rather than SQLite. The
storage shape follows the data: small validated maps use atomic marshal
snapshots; large immutable bytes use content-addressed files; reusable installs
use cloneable directory trees.

| Owner | Storage | Contents and validity |
| --- | --- | --- |
| `network/cache.py` | `http-v2/` | HTTP metadata/body pairs under hashed keys; missing or partial pairs are misses |
| `index/catalog_cache.py` | records in the HTTP cache | versioned parsed Simple API links keyed by source URL |
| `index/artifact_cache.py` | `artifacts-v1/` | immutable bodies by SHA-256 plus normalized-URL receipts and expected-hash validation |
| `index/candidate_cache.py` | `wheels/` | wheels built from source, keyed by stable source identity |
| `index/metadata_cache.py` | `metadata-v2.marshal` | parsed local wheel headers keyed by absolute path, size, and modification time |
| `index/candidate_metadata_cache.py` | `candidate-metadata-v2.marshal` | dependency metadata safe to reuse during resolution |
| `index/release_facts_cache.py` | `release-facts-v1.marshal` | deterministic release-level rejection reasons |
| `resolution/engine/sources/wheelhouse/` | wheelhouse catalog and metadata snapshots | local source identities, parsed metadata, and compatibility inputs |
| `cli/fast_install_cache.py` | `fast-install-v3.marshal` and `fast-install-trees-v1/` | narrow local plans, wheel metadata, and cloneable completed targets |
| `install/wheel_archive_cache.py` | `archive-v1/` | validated, unpacked immutable wheel trees keyed by wheel digest |
| `install/wheel_archive_cache.py` | `resolution-v2/` | short-lived exact-pin plan receipts referencing validated archives |

`core/marshal_cache.py` provides best-effort snapshot loading and atomic
replacement for the small persistent maps. Each cache owns its schema,
version, key validation, size limits, and value validation.

Cache invariants are:

1. A cache is optional. Missing, stale, corrupt, inaccessible, or semantically
   ineligible entries must become misses, not correctness failures.
2. Cache keys include every input that can change the reusable result:
   requirement, source, interpreter/target, policy, hashes, and relevant
   filesystem identity.
3. Immutable bodies and trees are published only after validation. Readers
   must never observe partially written entries.
4. Small snapshots are versioned and atomically replaced. Loading validates
   both the outer format and every retained key/value.
5. Exact install-plan receipts are deliberately narrow and short-lived. They
   reference digest-validated archive entries instead of trusting arbitrary
   wheel paths.
6. Cache ownership remains distributed. Do not introduce cross-cache
   transactions or a global mutable database without first defining failure,
   migration, locking, and invalidation semantics.

`cli/cache.py:CacheManager` is the command-facing manager for the major
directory-backed caches and fast-install snapshot generations. When a cache
family becomes user-visible, its inspection and purge behavior must be added
there explicitly.

## Package ownership and dependency direction

| Package | Owns | Should not own |
| --- | --- | --- |
| `core` | shared value types, packaging rules, hashes, URLs, wheels, cache primitives | command policy or orchestration |
| `platform` | configuration locations, install schemes, cloning, and host behavior | package-selection policy |
| `build` | backend hooks, metadata generation, build isolation, and build artifacts | resolver decisions |
| `index` | sources, links, catalogs, discovery, artifact localization, candidate materialization | dependency backtracking or installation |
| `network` | sessions, authentication, HTTP transport, progress, and HTTP cache | resolver state |
| `vcs` | VCS URL handling, revisions, and source retrieval | wheel selection |
| `resolution` | requirements, constraints, propagation, search, conflict learning, result assembly | filesystem installation |
| `install` | targets, inventories, archive preparation, extraction, direct URLs, transactions | index page parsing |
| `cli` | argument parsing, requirement collection, command dispatch, presentation, narrow command fast paths | reusable lower-level package mechanics |

The allowed runtime imports are declared in `pyproject.toml`:

| Domain | May import at runtime |
| --- | --- |
| `core` | none of the other domains |
| `platform` | `core` |
| `build` | `core`, `platform` |
| `index` | `core`, `build`, `platform` |
| `network` | `core`, `platform`, `build`, `index` |
| `vcs` | `core` |
| `resolution` | `core`, `index`, `network`, `vcs` |
| `install` | `core`, `platform`, `network`, `build`, `index`, `resolution`, `vcs` |
| `cli` | every lower domain |

`tests/test_workspace_boundaries.py` parses runtime imports, checks that every
edge is allowed, and checks the resulting graph for cycles. Imports guarded by
`TYPE_CHECKING` are intentionally excluded, so type-only dependencies still
require review. Vendored code is outside these first-party domains.

When a change appears to fit two packages, keep policy in the higher-level
owner and put only reusable mechanics in the lower-level package. A lower
domain must not import a higher domain merely to reuse a convenience type.

## Performance boundaries

These boundaries are part of behavior, not benchmark-only implementation
details:

1. `cli.entrypoint` imports only bootstrap data until it knows which route is
   needed. Help, version, and command help must not import the fallback graph.
2. Command registration remains lazy. Adding a command must not import its
   implementation during general startup.
3. Fast command paths recognize only semantics they implement completely and
   return `None` or `False` before unsupported behavior is committed.
4. Candidate discovery, metadata parsing, artifact localization, and source
   builds remain demand-driven. Replaying a `CandidateStream` must reuse work.
5. Remote winner materialization may run concurrently, and completed wheels
   may be pipelined into archive preparation, but final candidate and install
   order remain deterministic.
6. Persistent caches accelerate repeated work but never become the sole source
   of correctness. Formats are versioned where schemas matter, entries are
   validated for their storage model, and every cache is safe to ignore.
7. Installation optimizations preserve batch atomicity or explicitly decline
   to a route that does. A faster path must not weaken rollback behavior.
8. Optimizations should improve a semantic workload class, not recognize a
   benchmark fixture. Eligibility belongs at an existing architectural
   boundary rather than in package-name or corpus-specific special cases.

For performance work, first identify which boundary owns the repeated work.
Prefer reducing work within that owner, moving it behind an existing lazy
boundary, or caching a stable reusable result over adding a utility imported
by every command.

## Navigation recipes

For a CLI bug, begin at `cli/entrypoint.py` and determine whether a fast path
accepted the command. If not, inspect the command's `CommandSpec` and `run_*`
function.

For an install-planning bug, begin at `cli/commands/install.py`, identify which
of the cached, local-wheelhouse, or canonical resolver lanes produced the
plan, then inspect `resolution/engine/output.py` for winner preparation.

For a resolver bug, begin at `ResolutionEngine.resolve` and
`ResolutionRuntime.resolve_internal`. Determine whether `propagation.try_resolve`
returned a kernel result or the generic `SearchLoop` ran before descending into
selection, provider, conflict, or policy code.

For an index or metadata bug, follow `CandidateProvider` through link
evaluation into `CandidateMaterializer`. Start at `ArtifactLocator` only when
the issue involves turning a selected link into local bytes.

For an installation bug, follow the direction of execution:
`install.py` -> `output.prepare_install_candidates` ->
`wheel_transaction.install_wheels_transactionally`. Then determine whether the
archive, direct, or generic transaction route accepted the batch. Start at
`CandidateMaterializer.iter_materialize` for source-build failures.

For a cache bug, begin with the cache owner and identify the key, payload
version, validation, atomic publication, and fallback behavior. Do not begin by
assuming the HTTP cache, artifact cache, archive cache, and plan cache share a
transaction or invalidation policy.

Useful searches:

```sh
rg -n "run_cached_remote|run_local_fallback|run_fast_install|run_fast_list" src/cpip
rg -n "ResolutionEngine|resolve_internal|try_resolve|SearchLoop|ReleaseFrontier" src/cpip/resolution
rg -n "CandidateProvider|CandidateEvaluator|CandidateMaterializer|CandidateStream" src/cpip/index
rg -n "ArtifactLocator|ArtifactCache|prepare_install_candidates" src/cpip
rg -n "install_wheels_transactionally|install_wheels_from_archive_cache|install_wheels_directly" src/cpip
rg -n "load_snapshot|save_snapshot|load_cached_install_plan|save_cached_install_plan" src/cpip
```

Run the boundary tests after changing package ownership or imports:

```sh
uv run pytest tests/test_workspace_boundaries.py -q
```
