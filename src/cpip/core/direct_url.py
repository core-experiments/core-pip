from __future__ import annotations

import urllib.parse

DIRECT_URL_METADATA_NAME = "direct_url.json"


class DirectUrlValidationError(ValueError):
    pass


def expect_string(data: dict[str, object], key: str, field: str | None = None) -> str:
    value = data.get(key)
    label = field or key
    if not isinstance(value, str) or not value:
        raise DirectUrlValidationError(f"Missing required value in '{label}'")
    return value


class ArchiveInfo:
    __slots__ = ("hash", "hashes")

    def __init__(
        self,
        hash: str | None = None,
        hashes: dict[str, str] | None = None,
    ) -> None:
        self.hash = hash
        self.hashes = hashes

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ArchiveInfo:
        hash_value = data.get("hash")
        hashes = data.get("hashes")
        normalized_hashes: dict[str, str] | None = None
        if hash_value is not None:
            if not isinstance(hash_value, str):
                raise DirectUrlValidationError(
                    f"Unexpected type {type(hash_value).__name__} "
                    "(expected str) in 'archive_info.hash'",
                )
            hash_text = hash_value
            if "=" not in hash_text:
                raise DirectUrlValidationError(
                    "Invalid hash format (expected '<algorithm>=<hash>') in 'archive_info.hash'",
                )
            algorithm, digest = hash_text.split("=", 1)
            normalized_hashes = {algorithm: digest}
        if hashes is not None:
            if not isinstance(hashes, dict):
                raise DirectUrlValidationError(
                    f"Unexpected type {type(hashes).__name__} "
                    "(expected dict) in 'archive_info.hashes'",
                )
            normalized_hashes = {
                str(name): str(value) for name, value in hashes.items()
            }
        return cls(
            hash=hash_value if isinstance(hash_value, str) else None,
            hashes=normalized_hashes,
        )

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {}
        if self.hash is not None:
            data["hash"] = self.hash
        if self.hashes is not None:
            data["hashes"] = dict(self.hashes)
        return data


class DirInfo:
    __slots__ = ("editable",)

    def __init__(self, editable: bool = False) -> None:
        self.editable = editable

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DirInfo:
        editable = data.get("editable", False)
        if not isinstance(editable, bool):
            raise DirectUrlValidationError(
                "Unexpected type str (expected bool) in 'dir_info.editable'"
                if isinstance(editable, str)
                else f"Unexpected type {type(editable).__name__} (expected bool) in 'dir_info.editable'",
            )
        return cls(editable=editable)

    def to_dict(self) -> dict[str, object]:
        return {"editable": True} if self.editable else {}


class VcsInfo:
    __slots__ = ("commit_id", "requested_revision", "vcs")

    def __init__(
        self,
        vcs: str,
        commit_id: str,
        requested_revision: str | None = None,
    ) -> None:
        self.vcs = vcs
        self.commit_id = commit_id
        self.requested_revision = requested_revision

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> VcsInfo:
        vcs = expect_string(data, "vcs", "vcs_info.vcs")
        commit_id = expect_string(data, "commit_id", "vcs_info.commit_id")
        requested_revision = data.get("requested_revision")
        if requested_revision is not None and not isinstance(requested_revision, str):
            raise DirectUrlValidationError(
                f"Unexpected type {type(requested_revision).__name__} "
                "(expected str) in 'vcs_info.requested_revision'",
            )
        return cls(
            vcs=vcs,
            commit_id=commit_id,
            requested_revision=(
                requested_revision if isinstance(requested_revision, str) else None
            ),
        )

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"vcs": self.vcs, "commit_id": self.commit_id}
        if self.requested_revision is not None:
            data["requested_revision"] = self.requested_revision
        return data


class DirectUrl:
    __slots__ = ("archive_info", "dir_info", "info_subdir", "url", "vcs_info")

    def __init__(
        self,
        url: str,
        info_subdir: str | None = None,
        archive_info: ArchiveInfo | None = None,
        dir_info: DirInfo | None = None,
        vcs_info: VcsInfo | None = None,
    ) -> None:
        self.url = url
        self.info_subdir = info_subdir
        self.archive_info = archive_info
        self.dir_info = dir_info
        self.vcs_info = vcs_info

    @property
    def subdirectory(self) -> str | None:
        return self.info_subdir

    def is_local_editable(self) -> bool:
        return bool(self.dir_info and self.dir_info.editable)

    def validate(self) -> None:
        infos = [self.vcs_info, self.archive_info, self.dir_info]
        if sum(item is not None for item in infos) != 1:
            raise DirectUrlValidationError(
                "Exactly one of vcs_info, archive_info, dir_info must be present",
            )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DirectUrl:
        url = data.get("url")
        if not isinstance(url, str) or not url:
            raise DirectUrlValidationError("Missing required value in 'url'")
        archive_info = None
        dir_info = None
        vcs_info = None
        if "archive_info" in data:
            raw = data["archive_info"]
            if not isinstance(raw, dict):
                raise DirectUrlValidationError(
                    f"Unexpected type {type(raw).__name__} (expected dict) in 'archive_info'",
                )
            archive_info = ArchiveInfo.from_dict(raw)  # ty:ignore[invalid-argument-type]
        if "dir_info" in data:
            raw = data["dir_info"]
            if not isinstance(raw, dict):
                raise DirectUrlValidationError(
                    f"Unexpected type {type(raw).__name__} (expected dict) in 'dir_info'",
                )
            dir_info = DirInfo.from_dict(raw)  # ty:ignore[invalid-argument-type]
        if "vcs_info" in data:
            raw = data["vcs_info"]
            if not isinstance(raw, dict):
                raise DirectUrlValidationError(
                    f"Unexpected type {type(raw).__name__} (expected dict) in 'vcs_info'",
                )
            vcs_info = VcsInfo.from_dict(raw)  # ty:ignore[invalid-argument-type]
        direct_url = cls(
            url=url,
            archive_info=archive_info,
            dir_info=dir_info,
            vcs_info=vcs_info,
        )
        direct_url.validate()
        return direct_url

    @classmethod
    def from_json(cls, value: str) -> DirectUrl:
        import json

        return cls.from_dict(json.loads(value))

    def to_dict(self) -> dict[str, object]:
        self.validate()
        parsed = urllib.parse.urlsplit(self.url)
        redacted_url = self.url
        if parsed.scheme != "ssh" and "@" in parsed.netloc:
            auth, host = parsed.netloc.rsplit("@", 1)
            if not (auth.startswith("${") and auth.endswith("}")):
                if ":" in auth:
                    user, _, password = auth.partition(":")
                    if user.startswith("${") and user.endswith("}"):
                        user = ""
                    if password.startswith("${") and password.endswith("}"):
                        if self.vcs_info is not None:
                            redacted_url = urllib.parse.urlunsplit(
                                (
                                    parsed.scheme,
                                    parsed.netloc,
                                    parsed.path,
                                    parsed.query,
                                    parsed.fragment,
                                ),
                            )
                    else:
                        netloc = host if self.vcs_info is not None else parsed.netloc
                        redacted_url = urllib.parse.urlunsplit(
                            (
                                parsed.scheme,
                                netloc,
                                parsed.path,
                                parsed.query,
                                parsed.fragment,
                            ),
                        )
                elif not (auth.startswith("${") and auth.endswith("}")):
                    redacted_url = urllib.parse.urlunsplit(
                        (
                            parsed.scheme,
                            host,
                            parsed.path,
                            parsed.query,
                            parsed.fragment,
                        ),
                    )
        data: dict[str, object] = {
            "url": redacted_url,
        }
        if self.archive_info is not None:
            data["archive_info"] = self.archive_info.to_dict()
        if self.dir_info is not None:
            data["dir_info"] = self.dir_info.to_dict()
        if self.vcs_info is not None:
            data["vcs_info"] = self.vcs_info.to_dict()
        return data

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict_compat(), sort_keys=True)

    def as_pep440_direct_reference(self, name: str) -> str:
        suffix = (
            f"{self.vcs_info.vcs}+{self.url}@{self.vcs_info.commit_id}"
            if self.vcs_info is not None
            else self.url
        )
        fragments: list[str] = []
        if self.archive_info and self.archive_info.hashes:
            for algorithm, digest in sorted(self.archive_info.hashes.items()):
                fragments.append(f"{algorithm}={digest}")
        if self.info_subdir:
            fragments.append(f"subdirectory={self.info_subdir}")
        if fragments:
            suffix += "#" + "&".join(fragments)
        return f"{name} @ {suffix}"

    def to_dict_compat(self) -> dict[str, object]:
        """Serialize using the legacy single-hash field when needed."""
        data = self.to_dict()
        archive_info = data.get("archive_info")
        if isinstance(archive_info, dict):
            hashes = archive_info.get("hashes")
            if isinstance(hashes, dict) and hashes:
                algorithm, digest = next(iter(hashes.items()))
                if isinstance(algorithm, str) and isinstance(digest, str):
                    archive_info["hash"] = f"{algorithm}={digest}"  # ty:ignore[invalid-assignment]
                    del archive_info["hashes"]  # ty:ignore[invalid-argument-type]
        return data
