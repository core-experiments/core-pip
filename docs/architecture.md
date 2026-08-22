# cpip architecture

A map for finding code and keeping package boundaries. The implementation
named in each section is the source of truth; this page only says where to
look and which rules to keep.

## Start here

| Question | Start at |
| --- | --- |
| What happens when `cpip` starts? | `cli/entrypoint.py:main` |
| Where is a command implemented? | `cli/registry.py` → `run_*` in `cli/<name>.py` |
| How does install choose a plan? | `cli/install.py:run_install` |
| How are dependencies resolved? | `resolution/api.py:ResolutionEngine` → `nab_provider.py` → `_vendor/nab_resolver/` |
| Where do index candidates come from? | `index/provider.py:CandidateProvider` |
| How does an artifact become local? | `index/artifacts.py:ArtifactLocator` |
| How are selected candidates prepared? | `install/output.py:prepare_install_candidates` |
| How are wheels installed? | `install/wheel_transaction.py:install_wheels_transactionally` |
| Where do persistent caches live? | the owner in the cache table below; `cli/cache.py` for `cpip cache` |
| How are build backends invoked? | `build/build_backend.py:ProjectBuilder`, `build/build.py:build_wheel_from_source` |

## Process entry and dispatch

The console script, `cpip.__init__:main` and `python -m cpip` all reach
`cli.entrypoint:main`.

```text
cli.entrypoint:main
  +--> handle_global_commands      help, --version, --require-virtualenv gate, unknown commands
  +--> cli.fast:run_before_startup  cheap argv recognizers, before any startup work
  |      +--> run_lock
  |      +--> cli.fast_install:run_cached_remote   missing target + exact remote pins, warm receipt
  |      +--> cli.fast_install:run_local_fallback  non-empty local target, --no-index wheelhouse
  |      +--> cli.fast_install:run                 empty target, --no-index wheelhouse
  |      +--> run_satisfied_install                 plain names, all already installed
  |      +--> run_list                             no index options; sys.path or --path
  |      `--> run_freeze                           no -r/--user; no editables unless excluded
  +--> execution context, logging, temp dir (per CommandSpec flags)
  +--> cli.fast:run_install_after_startup / run_lock_after_startup
  `--> run_command -> CommandSpec.load_runner
```

Rules:

- Fast paths are recognizers, not separate semantics: they return `None` for
  any argument, target state or feature they do not implement completely, and
  normal dispatch always remains available afterwards. Keep recognition rules
  in `cli/fast.py` and `cli/fast_install.py`; `cli/entrypoint.py` must not name
  a command.
- The registry stores module paths and imports a command on first use. Startup
  gating belongs in `CommandSpec` flags (`needs_logging`, `needs_tempdir`,
  `needs_execution_context`), not in command-name tests.
- `cli/fast.py` and `cli/fast_install.py` are imported lazily and carry their
  own name normalization, requirement parsing, METADATA scanning and minimal
  wheelhouse resolver so a declined command pays only for the token tests. That
  duplication is deliberate; do not consolidate it into the shared
  implementations. The modules the fast install path does import keep their
  rarely-run dependencies (`email.parser`, `importlib.resources`,
  `concurrent.futures`, `resolution.models`) behind function-level imports;
  `tests/core/test_startup_imports.py` pins what that path may load.

Shared concerns inside `cli` have one owner each; extend the owner rather than
re-deriving locally:

| Concern | Owner |
| --- | --- |
| Config files, `CPIP_*` overrides, source selection | `cli/config.py` |
| Requirement collection, `--config-settings`, proxy environment | `cli/requirements.py` |
| `--group` and dependency-group files | `cli/dependency_groups.py` |
| Lock serialization (imports nothing; shared with fast paths) | `cli/lock_format.py` |
| Cache directory policy | `core/appdirs.py` |
| Resolver report → CLI diagnostic | `cli/resolution_errors.py` |

Known, deliberate divergences: `install` concatenates configured and
command-line find-links instead of using `resolve_sources`; the lock commands
use `configured_cache_dir` (opt-in caching) instead of `resolve_cache_dir`.

## Installation

```text
cli.install:run_install
  -> cli.requirements: roots, constraints, sources, policy
  -> plan: install.wheel_install_plan_cache:load_cached_install_plan (exact remote pins, warm)
           else ResolutionEngine.resolve
  -> install.output:prepare_install_candidates   materialize winners, prepare wheel archives
  -> cli.fast_install:install_resolved_pure_wheels (empty-target pure-wheel hybrid)
     else install.wheel_transaction:install_wheels_transactionally
  -> save_cached_install_plan after a fresh exact-pin install
  -> conflict warnings; editables go through build.build:build_editable_from_source
```

`cli/install.py` is the adapter between the plan shapes; command code and
installers depend only on the shared candidate attributes, never on resolver
internals. Source candidates become wheels during candidate materialization,
not at install time.

`install_wheels_transactionally` dispatches to three routes, each preserving
batch rollback: `install_wheels_from_archive_cache` (clone cached immutable
trees into a stage, swap the target), `install_wheels_directly` (after a full
destination preflight), and the generic staged `WheelInstaller` /
`InstallTransaction` path.

The pure-wheel hybrid requires an empty explicit target. That is a safety
precondition: it validates members with the lexical
`cli/fast_install.py:is_safe_member`, which is sound only because every member
is written as a regular file into an empty tree. The staged routes write into
populated targets and therefore use `install/wheel_archive.py:validate_member_parts`
plus a resolved-parent containment check. Relaxing the emptiness rule means
adopting the resolving check.

## Resolution

```text
ResolutionEngine.resolve                     resolution/api.py
  -> inputs.coerce_requirements
  -> NabProvider(CandidateProvider)          resolution/nab_provider.py
  -> nab_resolver.Resolver.resolve           _vendor/nab_resolver/ (vendored; the search itself)
  -> ResolutionResult                        resolution/models.py
```

`NabProvider` is the whole contract between cpip and the search
(`choose_version`, `get_dependencies`, `has_satisfying_version`, `prioritize`,
and the conflict-display hooks). Everything the resolver learns about the
index, installed state or policy arrives through it. On failure `api.py`
renders the resolver's error with `format_error` and restores the user's
original specifier text.

`resolve_wheelhouse` is only a constructor that pins the engine to local
`find_links` with the index disabled; there is no second search. The separate
minimal resolver in `cli/fast_install.py` exists for startup cost (see above)
and must not grow an implementation arrow to this one.

## Index discovery and artifacts

```text
CandidateProvider.find_candidates            index/provider.py
  -> source locations, Simple API / find-links catalogs (catalog_cache.py)
  -> Link records -> CandidateEvaluator, candidate_filters
  -> CandidateRecord -> CandidateMaterializer.iter_materialize
  -> CandidateStream[WheelCandidate]          replayable, advances on demand
```

`CandidateProvider` coordinates discovery; it owns neither backtracking nor
installation. `CandidateMaterializer` localizes artifacts only when metadata
or a selected winner needs them and builds sdists through
`build.build:build_wheel_from_source`.

`ArtifactLocator.ensure_local[_text]` is the one route from a link to local
bytes: local path → artifact cache (URL receipt or expected SHA-256) → HTTP
cache body → `NetworkSession` stream. `network/download.py:Downloader` is the
resumable/progress downloader for the preparer path, not this route.

## Caches

Every persisted cache lives under `<cache root>/v<CACHE_VERSION>/`
(`core/appdirs.py:versioned_cache_dir`, currently `v0/`), which
`resolve_cache_dir`/`configured_cache_dir` hand to every writer. That directory
is the only version the cache system has: no cache carries one in its name or
payload, bumping `core/utils.py:CACHE_VERSION` invalidates everything at once,
there is no migration code, and `cpip cache purge` removes every `v*`
directory without knowing the stores.

| Owner | Under `v0/` | Contents |
| --- | --- | --- |
| `network/cache.py` | `http/` | HTTP metadata/body pairs; a partial pair is a miss |
| `index/catalog_cache.py` | entries in `http/` | parsed Simple API catalogs, release summaries (`Version.to_wire()`), target choices; checksum-validated, recompiled from the catalog on any failure |
| `index/artifact_cache.py` | `artifacts/` | bodies by SHA-256 plus URL receipts |
| `index/candidate_cache.py` | `wheels/` | wheels built from source |
| `index/metadata_cache.py` | `metadata.sqlite` | parsed headers of local wheel files and of installed `METADATA` files, and SHA-256 of local wheels, by path, size, mtime |
| `index/candidate_metadata_cache.py` | `candidate-metadata.sqlite` | dependency metadata reused during resolution |
| `index/release_facts_cache.py` | `release-facts.marshal` | deterministic release rejection reasons |
| `cli/fast.py` | `fast-lock-plan/` | rendered lock output |
| `cli/fast_install.py` | `fast-install-<interp>.marshal`, `fast-install-trees/` | fast-path plans, metadata, cloneable completed targets |
| `install/wheel_archive_cache.py` | `archive-<interp>/` | validated unpacked wheel trees by digest |
| `install/wheel_install_plan_cache.py` | `resolution-<interp>/` | short-lived exact-pin receipts over archive entries |

`<interp>` is `core/utils.py:CACHE_INTERPRETER_TAG`: marshal data is not
portable across interpreters, so those buckets are scoped per interpreter.

Invariants:

1. A cache is optional. Missing, corrupt, inaccessible or ineligible entries
   are misses, never correctness failures; every load validates shape.
2. Keys include every input that can change the result: requirement, source,
   interpreter/target, policy, hashes, filesystem identity.
3. Entries are published only after validation, atomically; readers never see
   a partial write.
4. Caches stay independent. No cross-cache transactions or shared mutable
   database.

## Package ownership and dependency direction

| Package | Owns | Must not own |
| --- | --- | --- |
| `core` | value types, packaging rules, hashes, URLs, wheels, cache primitives | command policy |
| `platform` | config locations, install schemes, cloning, host behavior | selection policy |
| `build` | backend hooks, metadata generation, build isolation | resolver decisions |
| `index` | sources, links, catalogs, discovery, artifact localization, materialization | backtracking, installation |
| `network` | sessions, auth, HTTP transport, HTTP cache | resolver state |
| `vcs` | VCS URLs, revisions, source retrieval | wheel selection |
| `resolution` | requirements, constraints, search adapter, result assembly | filesystem installation |
| `install` | targets, inventories, archives, extraction, transactions | index parsing |
| `cli` | argument parsing, dispatch, presentation, fast paths | reusable mechanics |

Allowed runtime imports (enforced by review, not by a test):

| Domain | May import |
| --- | --- |
| `core` | nothing first-party |
| `platform` | `core` |
| `build` | `core`, `platform` |
| `index` | `core`, `build`, `platform` |
| `network` | `core`, `platform`, `build`, `index` |
| `vcs` | `core` |
| `resolution` | `core`, `index`, `network`, `vcs` |
| `install` | everything but `cli` |
| `cli` | everything |

Three edges cross the table and are known debt, not precedent:
`build/build_backend.py` → `install.build_env.venv`,
`resolution/inputs.py` → `install.requirement_set`,
`resolution/req_install.py` → `build.pep517_hooks`. `TYPE_CHECKING` imports
count as edges too; move the shared shape down instead of guarding the import.
Vendored code is outside these rules.

Check new edges by hand after changing a package:

```sh
rg -n "^from cpip\.|^import cpip\." src/cpip/<package> | grep -v "cpip\.<package>"
```

## Performance boundaries

1. Fast paths recognize only what they implement completely and decline before
   committing to unsupported behavior.
2. Discovery, metadata parsing, artifact localization and source builds stay
   demand-driven; replaying a `CandidateStream` reuses work.
3. Winner materialization and archive preparation may run concurrently, but
   candidate and install order stay deterministic.
4. Caches accelerate; they are never the sole source of correctness.
5. A faster install route preserves batch atomicity or declines to one that does.
6. Optimize a semantic workload class at an existing boundary, never a
   benchmark fixture or package name.
