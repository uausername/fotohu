"""Builds live backend instances from the rows in ``storage_accounts``.

It also owns the two pieces of glue every backend needs but should not know
about: writing rotated OAuth tokens back to the database, and giving
id-addressed providers a persistent folder cache.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..core.crypto import decrypt_json, encrypt_json
from ..core.errors import StorageError
from ..db.repo import Repo
from .base import AuthStart, BackendInfo, StorageBackend
from .gdrive import GoogleDriveBackend
from .local import LocalBackend
from .onedrive import OneDriveBackend
from .rclone_backend import RcloneBackend

log = logging.getLogger(__name__)

BACKENDS: dict[str, type[StorageBackend]] = {
    OneDriveBackend.info.key: OneDriveBackend,
    GoogleDriveBackend.info.key: GoogleDriveBackend,
    RcloneBackend.info.key: RcloneBackend,
    LocalBackend.info.key: LocalBackend,
}


def backend_choices() -> list[BackendInfo]:
    return [cls.info for cls in BACKENDS.values()]


def get_backend_class(key: str) -> type[StorageBackend]:
    try:
        return BACKENDS[key]
    except KeyError:
        raise StorageError(f"unknown storage backend: {key}") from None


class StorageRegistry:
    def __init__(
        self,
        repo: Repo,
        secret_key: str,
        rclone_binary: str = "rclone",
        rclone_config: str | None = None,
    ) -> None:
        self.repo = repo
        self.secret_key = secret_key
        self.rclone_binary = rclone_binary
        self.rclone_config = rclone_config

    # ------------------------------------------------------------- construction

    async def build(self, record: dict[str, Any]) -> StorageBackend:
        cls = get_backend_class(record["backend"])
        credentials: dict[str, Any] = {}
        if record.get("credentials_enc"):
            credentials = decrypt_json(self.secret_key, record["credentials_enc"])
        extra = json.loads(record.get("extra_json") or "{}")

        if cls is RcloneBackend:
            # Where the rclone binary and its config file live is a property of
            # the machine, not of the linked account, so it comes from the
            # environment every time rather than from the row. Older rows carry
            # a snapshot taken when the remote was linked; honouring it would
            # break the moment the database moved to another host — which is
            # exactly what happens when an install migrates to a server.
            extra = {
                **extra,
                "binary": self.rclone_binary,
                "config_path": self.rclone_config,
            }

        backend = cls(
            account_id=record["id"],
            root_folder=record["root_folder"],
            credentials=credentials,
            extra=extra,
        )
        self._attach_cache(backend)
        return backend

    async def get_default(self) -> StorageBackend | None:
        record = await self.repo.get_default_storage()
        return await self.build(record) if record else None

    async def get(self, account_id: int) -> StorageBackend | None:
        record = await self.repo.get_storage_account(account_id)
        return await self.build(record) if record else None

    def _attach_cache(self, backend: StorageBackend) -> None:
        repo, account_id = self.repo, backend.account_id

        async def cache_get(path: str) -> str | None:
            return await repo.get_cached_folder(account_id, path)

        async def cache_put(path: str, remote_id: str) -> None:
            await repo.put_cached_folder(account_id, path, remote_id)

        backend.cache_get = cache_get  # type: ignore[method-assign]
        backend.cache_put = cache_put  # type: ignore[method-assign]

    # ------------------------------------------------------------- persistence

    async def persist_credentials(self, backend: StorageBackend) -> None:
        """Write back tokens the backend rotated during its last call.

        Google rotates refresh tokens on some flows; dropping the new one means
        the family has to re-link the account, so this is not optional.
        """
        if not backend.credentials_dirty:
            return
        await self.repo.update_storage_account(
            backend.account_id,
            credentials_enc=encrypt_json(self.secret_key, backend.credentials),
        )
        backend.credentials_dirty = False

    async def save_credentials(self, account_id: int, credentials: dict[str, Any]) -> None:
        await self.repo.update_storage_account(
            account_id, credentials_enc=encrypt_json(self.secret_key, credentials)
        )

    # -------------------------------------------------------------------- OAuth

    @staticmethod
    def begin_auth(backend_key: str, redirect_uri: str, extra: dict | None = None) -> AuthStart:
        return get_backend_class(backend_key).begin_auth(redirect_uri, extra)

    @staticmethod
    async def finish_auth(
        backend_key: str, code: str, verifier: str | None, redirect_uri: str,
        extra: dict | None = None,
    ) -> dict[str, Any]:
        return await get_backend_class(backend_key).finish_auth(
            code, verifier, redirect_uri, extra
        )
