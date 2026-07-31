from __future__ import annotations

import datetime
from typing import Any


def create_candidate_provider(
    options: Any,
    bundle: Any,
    requirements: list[Any],
    build_options: dict[str, dict[str, object]],
    target: Any,
    *,
    cache_dir: str | None,
) -> Any:
    """Create the candidate provider and apply requirement/constraint hashes."""
    from cpip.cli.commands.install_helpers import intersect_hashes
    from cpip.core.hashes import Hashes
    from cpip.core.packaging import parse_requirement
    from cpip.index.links import Link
    from cpip.index.provider import CandidateProvider

    provider = CandidateProvider.from_options(
        find_links=bundle.find_links,
        index_url=bundle.index_url,
        extra_index_urls=bundle.extra_index_urls,
        no_index=bundle.no_index,
        format_control=bundle.format_control,
        build_options=build_options,
        build_constraints=options.build_constraint_files,
        wheel_cache_dir=cache_dir,
        trusted_hosts=options.trusted_hosts,
        session=bundle.session,
        dry_run=options.dry_run,
        build_isolation=not options.no_build_isolation,
        locked_links={name: Link(url) for name, url in bundle.locked_links.items()},
        target=target,
        uploaded_prior_to=(
            datetime.datetime.fromisoformat(
                options.uploaded_prior_to.replace("Z", "+00:00")
            )
            if options.uploaded_prior_to
            else None
        ),
    )
    provider.release_control = bundle.release_control
    provider.hashes_by_name = {}
    for item in requirements:
        if item.req is None or not item.hash_options:
            continue
        hashes = item.hashes()
        previous = provider.hashes_by_name.get(item.req.canonical_name)
        provider.hashes_by_name[item.req.canonical_name] = (
            hashes if previous is None else intersect_hashes(previous, hashes)
        )
    for raw, hashes in (
        *bundle.constraint_hashes.items(),
        *bundle.requirement_hashes.items(),
    ):
        name = parse_requirement(raw).canonical_name
        current = provider.hashes_by_name.get(name)
        provider.hashes_by_name[name] = (
            Hashes(hashes)
            if current is None
            else intersect_hashes(current, Hashes(hashes))
        )
    return provider
