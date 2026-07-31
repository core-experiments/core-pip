from __future__ import annotations

import datetime
import logging
import textwrap
import traceback
from collections.abc import Iterable, Sequence
from contextlib import nullcontext
from io import StringIO
from typing import TYPE_CHECKING, Protocol

from pip.build.metadata import InstalledDistributionStore

from pip.install.build_env.base import Prefix
from pip.core.errors import (
    DiagnosticPipError,
    PipError,
)
from pip.core.format_control import FormatControl
from pip.core.release_control import ReleaseControl
from pip.core.temp_dir import TempDirectory
from pip.install.requirements import installed_packages_summary

if TYPE_CHECKING:
    from pip.build.cache import WheelCache
    from pip.build.tracker import BuildTracker
    from pip.resolution.req_install import InstallRequirement
    from pip.network.http import NetworkSession


logger = logging.getLogger(__name__)


class NamedRequirement(Protocol):
    @property
    def name(self) -> str | None: ...


class BuildOptions(Protocol):
    format_control: FormatControl
    release_control: ReleaseControl | None
    index_urls: list[str]
    find_links: list[str]
    proxy: str | None
    no_proxy_env: bool
    trusted_hosts: tuple[str, ...]
    custom_cert: str | None
    client_cert: str | None
    prefer_binary: bool
    uploaded_prior_to: datetime.datetime | None
    session: NetworkSession


class BuildConfiguration:
    """Settings inherited by an isolated build dependency installation."""

    __slots__ = (
        "session", "format_control", "release_control", "index_urls", "find_links",
        "proxy", "no_proxy_env", "trusted_hosts", "custom_cert", "client_cert",
        "prefer_binary", "uploaded_prior_to",
    )

    def __init__(
        self, session: NetworkSession, format_control: FormatControl | None = None,
        release_control: ReleaseControl | None = None,
        index_urls: list[str] | None = None, find_links: list[str] | None = None,
        proxy: str | None = None, no_proxy_env: bool = False,
        trusted_hosts: tuple[str, ...] = (), custom_cert: str | None = None,
        client_cert: str | None = None, prefer_binary: bool = False,
        uploaded_prior_to: datetime.datetime | None = None,
    ) -> None:
        self.session = session
        self.format_control = format_control if format_control is not None else FormatControl()
        self.release_control = release_control if release_control is not None else ReleaseControl()
        self.index_urls = index_urls if index_urls is not None else []
        self.find_links = find_links if find_links is not None else []
        self.proxy = proxy
        self.no_proxy_env = no_proxy_env
        self.trusted_hosts = trusted_hosts
        self.custom_cert = custom_cert
        self.client_cert = client_cert
        self.prefer_binary = prefer_binary
        self.uploaded_prior_to = uploaded_prior_to


class InstallWheelBuildError(DiagnosticPipError):
    reference = "failed-wheel-build-for-install"

    def __init__(self, failed: list[InstallRequirement]) -> None:
        super().__init__(
            message=(
                "Failed to build installable wheels for some "
                "pyproject.toml based projects"
            ),
            context=", ".join(requirement.name or "" for requirement in failed),
            hint_stmt=None,
        )


class BuildDependencyInstallError(DiagnosticPipError):
    reference = "failed-build-dependency-install"

    def __init__(
        self,
        req: object | None,
        build_reqs: Iterable[str],
        *,
        cause: Exception,
        log_lines: list[str] | None,
    ) -> None:
        if isinstance(cause, PipError):
            note = "This is likely not a problem with pip."
        else:
            note = (
                "pip crashed unexpectedly. Please file an issue on pip's issue "
                "tracker: https://github.com/pypa/pip/issues/new"
            )

        message = "Cannot install build dependencies"
        if req:
            message += f" for {req}"
        if log_lines is None:
            context = "See above for more details."
        else:
            if isinstance(cause, PipError):
                log_lines.append(f"ERROR: {cause}")
            else:
                log_lines.extend(
                    "".join(traceback.format_exception(cause)).splitlines()
                )
            context = (
                f"Installing {' '.join(build_reqs)}\n"
                f"[{len(log_lines)} lines of output]\n"
                + "\n".join(log_lines)
                + "\n[end of output]"
            )
        super().__init__(
            message=message, context=context, hint_stmt=None, note_stmt=note
        )


class InprocessBuildEnvironmentInstaller:
    """
    Install build dependencies via the already running pip process.

    This contains a stripped down version of the install command with
    only the logic necessary for installing build dependencies. The
    finder, session, build tracker, and wheel cache are reused, but new
    instances of everything else are created as needed.

    Options are inherited from the parent install command unless
    they don't make sense for build dependencies (in which case, they
    are hard-coded, see comments below).
    """

    def __init__(
        self,
        *,
        options: BuildOptions,
        build_tracker: BuildTracker,
        wheel_cache: WheelCache,
        build_constraints: Sequence[InstallRequirement] = (),
        verbosity: int = 0,
    ) -> None:
        from pip.install.preparer import RequirementPreparer
        from pip.index.provider import CandidateProvider

        self.build_constraints_internal = build_constraints
        self.wheel_cache_internal = wheel_cache
        self.provider_internal = CandidateProvider.from_options(
            find_links=options.find_links,
            index_url=options.index_urls[0] if options.index_urls else None,
            extra_index_urls=options.index_urls[1:],
            no_index=not options.index_urls,
            format_control=options.format_control,
            prefer_binary=options.prefer_binary,
        )

        build_dir = TempDirectory(kind="build-env-install", globally_managed=True)
        self.preparer_internal = RequirementPreparer(
            build_isolation_installer=self,
            # Inherited options or state.
            session=options.session,
            build_dir=build_dir.path,
            build_tracker=build_tracker,
            verbosity=verbosity,
            # This is irrelevant as it only applies to editable requirements.
            src_dir="",
            # Hard-coded options (that should NOT be inherited).
            download_dir=None,
            build_isolation="venv",
            check_build_deps=False,
            progress_bar="off",
            # TODO: hash-checking should be extended to build deps, but that is
            # deferred for later as it'd be a breaking change.
            require_hashes=False,
            lazy_wheel=False,
        )

    def install(
        self,
        requirements: Iterable[str],
        prefix: Prefix,
        *,
        kind: str,
        for_req: InstallRequirement | None,
    ) -> None:
        """Install entrypoint. Manages output capturing and error handling."""
        capture_ctx = nullcontext(StringIO())
        logger.info("Installing %s ...", kind)

        try:
            with capture_ctx as stream:
                self.install_impl(requirements, prefix)

        except DiagnosticPipError as exc:
            # Format similar to a nested subprocess error, where the
            # causing error is shown first, followed by the build error.
            logger.info(textwrap.dedent(stream.getvalue()))
            logger.error("%s", exc)
            logger.info("")
            raise BuildDependencyInstallError(
                for_req, requirements, cause=exc, log_lines=None
            )

        except Exception as exc:
            logs: list[str] | None = textwrap.dedent(stream.getvalue()).splitlines()
            if isinstance(exc, PipError):
                logger.error("%s", exc)
            else:
                logger.exception("pip crashed unexpectedly")
            raise BuildDependencyInstallError(
                for_req, requirements, cause=exc, log_lines=logs
            )

    def install_impl(self, requirements: Iterable[str], prefix: Prefix) -> None:
        """Core build dependency install logic."""
        from pip.install.requirements import RequirementInstaller
        from pip.resolution.req_install import install_req_from_line

        from pip.install.wheel_builder import WheelBuilder

        ireqs = [install_req_from_line(req, user_supplied=True) for req in requirements]
        ireqs.extend(self.build_constraints_internal)

        from pip.resolution.resolver import Resolver

        resolver = Resolver(provider=self.provider_internal, ignore_installed=True)
        resolved_set = resolver.resolve_requirement_set(ireqs)
        self.preparer_internal.prepare_linked_requirements_more(
            resolved_set.requirements.values()
        )

        reqs_to_build = [
            r for r in resolved_set.requirements_to_install if not r.is_wheel
        ]
        _, build_failures = WheelBuilder(self.wheel_cache_internal, verify=True).build(
            reqs_to_build
        )
        if build_failures:
            raise InstallWheelBuildError(build_failures)

        installed = RequirementInstaller(
            root=None,
            home=None,
            prefix=prefix.path,
            use_user_site=False,
            pycompile=False,
            script_executable=prefix.python_executable,
        ).install_all(
            resolver.get_installation_order(resolved_set),
        )

        env = InstalledDistributionStore(paths=list(prefix.lib_dirs)).iter()
        if summary := installed_packages_summary(installed, env):
            logger.info(summary)
