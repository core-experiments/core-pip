# cpip architecture

This page is the map for finding code and preserving architectural boundaries.
It describes the current runtime paths, their owners, and the invariants that
performance work must retain.

The source of truth for runtime behavior is the implementation named in each
section. The allowed package dependencies below are a **convention, not a
checked constraint**: nothing in the build or the test suite currently enforces
them, so an import that crosses a boundary will land unless a reviewer catches
it. Treat the tables here as the thing to read before adding a cross-package
import, and say so in review when one is added.

## Start here

| Question | Start at | Then follow |
| --- | --- | --- |
| What happens when `cpip` starts? | `src/cpip/cli/entrypoint.py:main` | bootstrap output, narrow fast paths, fallback dispatch |
| Where is a command implemented? | `src/cpip/cli/registry.py` | its `CommandSpec` and the `run_*` function in `cli/<name>.py` |
| How does install choose a plan? | `src/cpip/cli/install.py:run_install` | `install_plan.py`, cached plans, wheelhouse plans, `ResolutionEngine` |
| How are dependencies resolved? | `src/cpip/resolution/api.py:ResolutionEngine` | `nab_provider.py`, then `_vendor/nab_resolver/` |
| Where do index candidates come from? | `src/cpip/index/provider.py:CandidateProvider` | sources, link evaluation, lazy materialization |
| How does an artifact become local? | `src/cpip/index/artifacts.py:ArtifactLocator` | artifact cache, HTTP cache, network session |
| How are selected candidates prepared? | `src/cpip/install/output.py:prepare_install_candidates` | candidate materialization and wheel archive preparation |
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
       +--> command help, for `<command> --help`
       +--> cli.entrypoint:handle_global_commands
              +--> no arguments, --help, `help [command]`, --version
              +--> the --require-virtualenv gate
              `--> unknown command names
       +--> cli.fast:run_before_startup
              +--> cli.fast.lock:run
              +--> cli.fast.install:run_cached_remote
              +--> cli.fast.install:run_local_fallback
              +--> cli.fast.install:run
              `--> cli.fast.list:run
       +--> configure execution context and logging when required
       +--> cli.fast:run_install_after_startup, when no install fast path ran
       +--> create a global temporary-directory context when required
       `--> cli.entrypoint:run_command
              +--> cli.fast:run_lock_after_startup, then the lock command
              `--> CommandSpec.load_runner
```

`handle_global_commands` runs before any fast path, so every command --
including the ones a fast path is about to accept -- passes the
`--require-virtualenv` gate. It answers help and version *before* that gate,
so `cpip --require-virtualenv --help` still works outside a virtualenv.

The entrypoint imports the command registry at module load, but the registry
holds only module *paths*: `CommandSpec.module` calls `importlib.import_module`
on first access, so importing `cpip.cli.registry` costs 7 `cpip` modules, not
14 command modules. Startup pays for a command only once dispatch reaches it.
Fast paths remain conservative recognizers, not separate command semantics.
They return `None` when an argument, target state, source shape, or feature is
not supported. Normal dispatch must remain available after every declined fast
path.

Within a command module, imports are top level: the resolver, installer, index,
and build subtrees load when their owning module loads, and no command hoists
`from X import Y` into a function to hide that cost. The exceptions are
deliberate and confined to the startup path -- `cli.entrypoint` and
`cli.fast.__init__` defer their imports so a declined command pays only for the
token tests, and VCS backends and PEP 517 build backends are resolved by name
at runtime. `CommandSpec.needs_logging`, `needs_tempdir`, and
`needs_execution_context` keep unnecessary startup work out of lightweight
commands; prefer adding a spec field over testing a command name in
`cli.entrypoint`. New process-level behavior belongs in `cli.entrypoint`; new
command behavior belongs in its command module and `CommandSpec`.

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

`cli.fast.list` and `cli.fast.lock` follow the same recognition rule: handle
only the declared subset and return control to normal command dispatch
otherwise.

The `cli.fast` package owns both halves of a fast path: the cheap argv gates in
its `__init__.py` and the parsers behind them. `cli.entrypoint` calls
`run_before_startup` once, asks `suppresses_logging` whether a quiet fast shape
means logging should be skipped, and retries `run_install_after_startup` /
`run_lock_after_startup` once startup has completed. Keep new recognition rules
in `cli.fast`; `cli.entrypoint` should not name a command. The package stays
import-light so a declined command pays for nothing but the token tests, and
each fast path module is imported only once its shape matches.

### Shared ownership inside `cli`

Every command reaches for the same small set of things. Each has exactly one
owner; add to the owner rather than re-deriving locally.

| Concern | Owner |
| --- | --- |
| Config files, `CPIP_*` overrides, and where sources come from | `cli/config.py` (`ConfigurationStore`, `SourceConfig`, `load_source_config`, `resolve_sources`) |
| Requirement collection, `--config-settings`, proxy environment | `cli/requirements.py` |
| `--group` splitting and dependency-group files | `cli/dependency_groups.py` |
| Lock serialization and lock output | `cli/lock_format.py` (imports nothing, so fast paths can share it) |
| Cache directory policy | `core/appdirs.py` (`resolve_cache_dir`, `configured_cache_dir`) |
| Resolver report to CLI diagnostic | `cli/resolution_errors.py` |
| Installed package sets for conflict checks | `build/check.py` |

`install` deliberately does not use `resolve_sources`: it concatenates
configured and command-line find-links and gates the index URL on whether one
was passed explicitly. The lock commands deliberately use
`configured_cache_dir`, not `resolve_cache_dir`: their caching is opt-in.
Divergences like these are documented at the call site rather than merged away.

`cli/fast/*` re-implements name normalization, requirement parsing, METADATA
scanning, JSON escaping, and a minimal wheelhouse resolver. That duplication is
deliberate -- it buys startup time by never importing `core.wheel`,
`build.metadata`, `index`, `resolution`, `json`, or `email.parser` -- and
should not be consolidated into the shared implementations.

## Installation planning

The ordinary non-editable dependency batch follows three planning lanes in
priority order:

```text
cli.install:run_install
  -> cli.requirements: collect roots, constraints, sources, and policy
  -> plan selection
       +--> exact remote plan receipt -> CachedInstallPlan
       +--> local pure-wheel adapter  -> InstallPlan
       `--> ResolutionEngine.resolve -> ResolutionResult
  -> deduplicate selected candidates
  -> install.output:prepare_install_candidates
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
the public API small and delegates the search itself to the vendored
`nab_resolver`. cpip retains ownership of candidate discovery, metadata, and
artifact materialization and exposes them to the search through a provider
adapter.

```text
ResolutionEngine.resolve                       (resolution/api.py)
  -> inputs.coerce_requirements                (root requirement coercion)
  -> NabProvider(self.provider, context=...)   (resolution/nab_provider.py)
       `--> wraps CandidateProvider            (index/provider.py)
  -> NabProvider.add_roots
  -> nab_resolver.resolver.Resolver.resolve    (_vendor/nab_resolver/)
       +--> Resolver asks the adapter for versions and dependencies
       `--> on failure: nab_resolver.report.format_error
  -> ResolutionResult / ResolvedRequirement    (resolution/model.py)
```

`NabProvider` is the whole contract between cpip and the search. It answers
`choose_version`, `get_dependencies`, `has_satisfying_version`, and
`prioritize`, and it owns the conflict-reporting hooks (`narrow_for_display`,
`widen_decision`, `consume_pending_clauses`). Anything the resolver needs to
know about the index, installed state, or policy arrives through it.

When the search fails, `nab_resolver` raises its own `ResolutionError` carrying
an incompatibility. `api.py` renders it with `format_error` and then restores
the user's original specifier text, because the provider models dependency
ranges as the finite set of available versions and an empty set would otherwise
print `<empty>` instead of the requirement the user typed.

### Resolver module ownership

- `api.py` owns the public `ResolutionEngine` façade, root coercion, error
  rendering, and conversion to `ResolutionResult`. It does not own the search.
- `nab_provider.py` owns the adapter: candidate lookup, version choice,
  dependency exposure, prioritization, and display narrowing.
- `config.py` owns `ResolutionConfig`, the resolution policy value object.
- `model.py` owns source-independent public result values.
- `inputs.py`, `input_models.py`, `input_paths.py`, and `input_requirements.py`
  own requirement coercion, input contracts, and path/URL requirement forms.
- `files/` owns requirements-file and pylock parsing (`parser.py`,
  `pylock.py`, `options.py`, `models.py`, `contracts.py`).
- `archive.py` owns wheelhouse availability signalling
  (`WheelhouseUnavailable`).
- `_vendor/nab_resolver/` owns the search itself: propagation, decisions,
  conflict learning, ranges, and error reporting. Treat it as vendored code.

Note that `install/output.py` — not this package — owns source-hash
finalization, winner materialization, and pipelined wheel archive preparation.

## Local wheelhouse resolution

There are two intentionally separate local-wheel implementations. Do not draw
an implementation arrow between them.

The process-level fast installer uses a minimal resolver:

```text
cli.fast.install:run
  -> cli.fast.install:resolve_simple_wheelhouse
       +--> scan wheel filenames
       +--> read selected wheel metadata through WheelArchive
       +--> recursively satisfy the supported requirement subset
       `--> reuse FastInstallMetadataCache plans and metadata
  -> cli.fast.install:install_resolved_pure_wheels
  -> cache a cloneable install tree after success
```

On a warm local install, the same path can clone a previously validated install
tree without resolving or extracting the wheels again.

The canonical engine uses the richer wheelhouse source:

```text
ResolutionEngine.resolve_wheelhouse            (resolution/api.py)
  -> ResolutionEngine(find_links=..., no_index=True, ignore_installed=True)
  -> ResolutionEngine.resolve                  (the ordinary nab path)
       -> ResolutionResult
```

There is no separate wheelhouse search. `resolve_wheelhouse` is a thin
convenience constructor: it pins the engine to local `find_links` with the
index disabled and then runs the same resolution as everything else. The
process-level resolver in `cli/fast/install.py` exists to keep startup and
object construction out of a deliberately narrow command shape, which is why
the two implementations stay separate.

`install_resolved_pure_wheels` accepts only the small candidate shape needed by
the shortcut (`canonical_name` and `path`). That shape is owned by
`core.wheel.PureWheelCandidate`, which both `core.wheel.WheelCandidate` and the
fast installer's own candidate satisfy. It is a compatibility seam, not a
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

The empty-target requirement is a safety precondition, not only an eligibility
rule. Member-name validation exists in two strengths: the staged routes use
`install/wheel_archive.py:validate_member_parts` together with a resolved-parent
containment check, because they write into populated targets where a path
component may already be a symlink. The hybrid uses the cheaper lexical
`cli/fast/install.py:is_safe_member`, which is sound only because the target is
known empty and every member is written as a regular file. Relaxing the
emptiness requirement means adopting the resolving check.

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
| `index/metadata_cache.py` | `metadata-v2.sqlite` | parsed local wheel headers keyed by absolute path, size, and modification time |
| `index/candidate_metadata_cache.py` | `candidate-metadata-v2.marshal` | dependency metadata safe to reuse during resolution |
| `index/release_facts_cache.py` | `release-facts-v1.marshal` | deterministic release-level rejection reasons |
| `cli/fast/install_cache.py` | `fast-install-v3.marshal` and `fast-install-trees-v1/` | narrow local plans, wheel metadata, and cloneable completed targets |
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

The allowed runtime imports are:

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

This table is enforced by review, not by a test. Vendored code is outside these
first-party domains.

Type-only imports deserve the same scrutiny as runtime ones. A `TYPE_CHECKING`
guard hides a boundary crossing from import-time behavior but not from the
design: `core/wheel.py` carried a `TYPE_CHECKING` import of a candidate class
from `cli` for exactly this reason, and the fix was to move the shared shape
down into `core`, not to leave the edge guarded.

When a change appears to fit two packages, keep policy in the higher-level
owner and put only reusable mechanics in the lower-level package. A lower
domain must not import a higher domain merely to reuse a convenience type.

## Performance boundaries

These boundaries are part of behavior, not benchmark-only implementation
details:

1. First-party `cpip` modules use top-level imports throughout, and
   `cli.entrypoint` eagerly loads the command registry, which in turn eagerly
   imports every command module. A `cpip` process therefore imports the full
   first-party graph (resolver, installer, index, platform, build, network) on
   startup, including for `--help`/`--version`. The only work that may remain
   deferred is genuinely non-first-party or dynamic: optional dependencies
   (`keyring`, `virtualenv`/`venv`), VCS backends selected by scheme, and PEP 517
   build backends selected by config name.
2. Command *modules* are loaded eagerly by the registry, so adding a command is
   part of the steady-state import graph and must respect the domain-ownership
   rules in the package-ownership table. Only dynamic dispatch — VCS backends by
   scheme and build backends by config name — remains import-deferred.
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

For an install-planning bug, begin at `cli/install.py`, identify which
of the cached, local-wheelhouse, or canonical resolver lanes produced the
plan, then inspect `install/output.py` for winner preparation.

For a resolver bug, begin at `ResolutionEngine.resolve` and the `NabProvider`
adapter. Determine whether the wrong answer came from candidate discovery
(`index/provider.py`) or from the search itself before descending into
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
rg -n "run_before_startup|run_install_after_startup|run_cached_remote|run_local_fallback" src/cpip
rg -n "ResolutionEngine|NabProvider|resolve_wheelhouse|coerce_requirements" src/cpip/resolution
rg -n "CandidateProvider|CandidateEvaluator|CandidateMaterializer|CandidateStream" src/cpip/index
rg -n "ArtifactLocator|ArtifactCache|prepare_install_candidates" src/cpip
rg -n "install_wheels_transactionally|install_wheels_from_archive_cache|install_wheels_directly" src/cpip
rg -n "load_snapshot|save_snapshot|load_cached_install_plan|save_cached_install_plan" src/cpip
```

After changing package ownership or imports, check the new edges against the
dependency table above by hand -- there is no boundary test to run:

```sh
rg -n "^from cpip\.|^import cpip\." src/cpip/<changed-package> | \
  grep -v "cpip\.<changed-package>"
```
