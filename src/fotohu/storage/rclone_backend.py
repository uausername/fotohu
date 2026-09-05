"""Generic backend driven by rclone.

This is what makes "or any other online drive" true: rclone speaks Box, Dropbox,
pCloud, Yandex.Disk, Mega, WebDAV, S3 and dozens more. The admin configures a
remote once with ``rclone config``, then points FotoHu at ``remote:path``.

We drive it through a long-lived ``rclone rcd`` daemon rather than by spawning
the command each time — see :mod:`fotohu.storage.rclone_daemon` for why that is
worth a background process. The operations below are therefore RC calls, but the
work they ask for is exactly what the equivalent CLI commands would have done.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.errors import StorageError
from ..core.models import LocalFile, Quota, RemoteFile
from .base import BackendInfo, StorageBackend
from .rclone_daemon import CALL_TIMEOUT, RcloneDaemon, RcloneRemoteError

log = logging.getLogger(__name__)

#: A 2 GB video over a slow uplink still has to fit.
TRANSFER_TIMEOUT = 3600


class RcloneBackend(StorageBackend):
    info = BackendInfo(
        key="rclone",
        title="Другое облако (через rclone)",
        needs_oauth=False,
        description="Box, Dropbox, pCloud, Яндекс.Диск, WebDAV, S3 и ещё ~70 провайдеров. "
                    "Настраивается командой `rclone config`, боту нужно только имя remote.",
    )

    def __init__(self, account_id: int, root_folder: str, credentials: dict[str, Any],
                 extra: dict[str, Any] | None = None,
                 daemon: RcloneDaemon | None = None) -> None:
        super().__init__(account_id, root_folder, credentials, extra)
        self.binary = self.extra.get("binary") or "rclone"
        self.config_path = self.extra.get("config_path")
        remote = (self.extra.get("remote") or "").strip()
        if remote and not remote.endswith(":") and ":" not in remote:
            remote = f"{remote}:"
        self.remote = remote
        #: The registry hands every backend the one daemon the process shares.
        #: A backend built outside it — a test, a one-off script — gets its own
        #: and is the only thing allowed to shut it down again.
        self._own_daemon = daemon is None
        self.daemon = daemon or RcloneDaemon(self.binary, self.config_path)
        #: remote_dir -> {filename: listing entry}. One backend exists per upload,
        #: so this is a within-upload cache and cannot go stale under us.
        self._listing: dict[str, dict[str, dict[str, Any]]] = {}

    # -------------------------------------------------------------------- plumbing

    async def _rc(
        self, path: str, payload: dict[str, Any], *, timeout: int = CALL_TIMEOUT
    ) -> dict[str, Any]:
        if not self.remote:
            raise StorageError("no rclone remote configured for this storage account")
        return await self.daemon.call(path, payload, timeout=timeout)

    def _remote_path(self, path: str) -> str:
        return f"{self.remote}{path.strip('/')}"

    async def _list_dir(self, remote_dir: str) -> dict[str, dict[str, Any]]:
        """Every name in one directory, fetched once.

        ``_free_filename`` asks about one candidate name after another, and each
        question used to be its own round trip. Listing the directory answers all
        of them at once, and is cheaper even for the first question: measured
        cold against OneDrive, a listing cost 4.0 s where a single stat cost 6.9 s.
        """
        if remote_dir in self._listing:
            return self._listing[remote_dir]
        try:
            body = await self._rc(
                "operations/list",
                {
                    "fs": self.remote,
                    "remote": remote_dir.strip("/"),
                    "opt": {"showHash": True, "noMimeType": True},
                },
            )
        except RcloneRemoteError as exc:
            if exc.status != 404:
                raise
            body = {}  # this month's folder simply does not exist yet
        index = {item["Name"]: item for item in body.get("list") or []}
        self._listing[remote_dir] = index
        return index

    @staticmethod
    def _to_remote(data: dict[str, Any], path: str) -> RemoteFile:
        raw = {k.lower(): v for k, v in (data.get("Hashes") or {}).items()}
        hashes = {}
        for ours, theirs in (
            ("sha256", "sha-256"), ("sha1", "sha-1"), ("md5", "md5"), ("quickxor", "quickxor")
        ):
            value = raw.get(theirs) or raw.get(ours)
            if value:
                hashes[ours] = value
        return RemoteFile(
            remote_id=data.get("ID") or path,
            path=path,
            size=data.get("Size"),
            hashes=hashes,
        )

    # --------------------------------------------------------------- operations

    async def ensure_folder(self, path: str) -> str:
        await self._rc("operations/mkdir", {"fs": self.remote, "remote": path.strip("/")})
        return self._remote_path(path)

    async def exists(self, remote_dir: str, filename: str) -> RemoteFile | None:
        item = (await self._list_dir(remote_dir)).get(filename)
        return self._to_remote(item, f"{remote_dir}/{filename}") if item else None

    async def stat(self, path: str) -> RemoteFile | None:
        body = await self._rc(
            "operations/stat",
            {"fs": self.remote, "remote": path.strip("/"), "opt": {"showHash": True}},
        )
        item = body.get("item")
        return self._to_remote(item, path) if item else None

    async def upload(self, local: LocalFile, remote_dir: str, filename: str) -> RemoteFile:
        target = f"{remote_dir.strip('/')}/{filename}"
        await self._rc(
            "operations/copyfile",
            {
                "srcFs": str(local.path.parent),
                "srcRemote": local.path.name,
                "dstFs": self.remote,
                "dstRemote": target,
            },
            timeout=TRANSFER_TIMEOUT,
        )
        self._listing.pop(remote_dir, None)  # we just changed what is in there

        stored = await self.stat(target)
        if stored is None:
            raise StorageError(
                f"rclone reported success but {self._remote_path(target)} is not there"
            )
        # rclone compares digests as part of copying and deletes the destination
        # if they differ, so a copy that returned at all is one the provider's own
        # hash agreed with — provided the provider reports a hash to agree with.
        stored.verified_on_write = bool(stored.hashes)
        return stored

    async def quota(self) -> Quota | None:
        try:
            data = await self._rc("operations/about", {"fs": self.remote})
        except StorageError:
            return None  # plenty of remotes simply do not implement `about`
        return Quota(total=data.get("total"), used=data.get("used"))

    async def check(self) -> str:
        await self._rc("operations/list", {"fs": self.remote, "remote": ""})
        await self.ensure_folder(self.root_folder)
        return f"rclone {self.remote} ok"

    async def close(self) -> None:
        if self._own_daemon:
            await self.daemon.stop()
