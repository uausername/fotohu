"""The storage backend contract.

Adding a new cloud means implementing :class:`StorageBackend` and registering it
in :mod:`fotohu.storage.registry` — nothing else in the project changes.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from ..core.models import LocalFile, Quota, RemoteFile


@dataclass(slots=True)
class AuthStart:
    """What the admin needs in order to link an account."""

    url: str
    verifier: str | None = None
    #: Shown instead of a link when the backend uses a device/manual flow.
    instructions: str | None = None


@dataclass(slots=True)
class BackendInfo:
    key: str
    title: str
    #: True when linking goes through a browser redirect and therefore needs
    #: FOTOHU_PUBLIC_URL to be reachable.
    needs_oauth: bool = True
    description: str = ""


class StorageBackend(abc.ABC):
    """One configured cloud destination."""

    info: BackendInfo

    def __init__(self, account_id: int, root_folder: str, credentials: dict[str, Any],
                 extra: dict[str, Any] | None = None) -> None:
        self.account_id = account_id
        self.root_folder = root_folder
        self.credentials = credentials
        self.extra = extra or {}
        #: Set by the registry when credentials are refreshed, so the caller can
        #: persist the new token set.
        self.credentials_dirty = False
        #: Path -> provider id. The registry swaps these for DB-backed versions so
        #: that id-addressed providers (Google Drive) keep their tree across restarts.
        self._folder_memo: dict[str, str] = {}

    async def cache_get(self, path: str) -> str | None:
        return self._folder_memo.get(path)

    async def cache_put(self, path: str, remote_id: str) -> None:
        self._folder_memo[path] = remote_id

    # ------------------------------------------------------------------ linking

    @classmethod
    def begin_auth(cls, redirect_uri: str, extra: dict[str, Any] | None = None) -> AuthStart:
        raise NotImplementedError(f"{cls.__name__} does not use OAuth")

    @classmethod
    async def finish_auth(
        cls, code: str, verifier: str | None, redirect_uri: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(f"{cls.__name__} does not use OAuth")

    # ---------------------------------------------------------------- operations

    @abc.abstractmethod
    async def ensure_folder(self, path: str) -> str:
        """Create ``path`` (recursively) if needed; return the provider's handle."""

    @abc.abstractmethod
    async def upload(self, local: LocalFile, remote_dir: str, filename: str) -> RemoteFile:
        """Upload and return the stored file, including any hashes the provider reports."""

    @abc.abstractmethod
    async def exists(self, remote_dir: str, filename: str) -> RemoteFile | None:
        """Return the file if that exact name is already taken, else ``None``."""

    async def quota(self) -> Quota | None:
        return None

    async def check(self) -> str:
        """Human-readable connectivity probe, used by the admin 'test' button."""
        await self.ensure_folder(self.root_folder)
        return "ok"

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------------- helpers

    def verify(self, local: LocalFile, remote: RemoteFile) -> bool | None:
        """Compare digests. ``None`` when the provider reports none we can use."""
        hashes = {k.lower(): v.lower() for k, v in (remote.hashes or {}).items() if v}
        if sha := hashes.get("sha256"):
            return sha == local.sha256.lower()
        if sha1 := hashes.get("sha1"):
            return bool(local.sha1) and sha1 == local.sha1.lower()
        if md5 := hashes.get("md5"):
            return md5 == local.md5.lower()
        if remote.size is not None:
            # Weakest fallback: at least prove nothing was truncated.
            return remote.size == local.size
        return None
