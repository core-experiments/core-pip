from __future__ import annotations

import os

from cpip.core.direct_url import ArchiveInfo, DirectUrl, DirInfo, VcsInfo
from cpip.core.urls import url_to_path
from cpip.index.links import Link
from cpip.vcs.versioncontrol import vcs


def direct_url_from_link(
    link: Link,
    *,
    source_dir: str | None = None,
    link_is_in_wheel_cache: bool = False,
) -> DirectUrl:
    if link.is_vcs:
        vcs_backend = vcs.get_backend_for_scheme(link.scheme)
        assert vcs_backend
        url, requested_revision, _ = vcs_backend.get_url_rev_and_auth(
            link.url_without_fragment
        )
        subdirectory = link.subdirectory_fragment
        commit_id = None
        if source_dir and os.path.exists(source_dir):
            source_backend = vcs.get_backend_for_dir(source_dir)
            assert source_backend
            commit_id = source_backend.get_revision(source_dir)
        elif link_is_in_wheel_cache and requested_revision is not None:
            commit_id = requested_revision
        else:
            commit_id = "HEAD"
        return DirectUrl(
            url=url,
            info_subdir=subdirectory,
            vcs_info=VcsInfo(
                vcs=vcs_backend.name,
                commit_id=commit_id,
                requested_revision=requested_revision,
            ),
        )
    if link.is_file and os.path.isdir(url_to_path(link.url)):
        return DirectUrl(url=link.url, dir_info=DirInfo())
    return DirectUrl(url=link.url, archive_info=ArchiveInfo(hashes=link.hashes or None))
