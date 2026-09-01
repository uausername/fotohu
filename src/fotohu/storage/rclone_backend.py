"""Generic backend driven by rclone.

This is what makes "or any other online drive" true: rclone speaks Box, Dropbox,
pCloud, Yandex.Disk, Mega, WebDAV, S3 and dozens more. The admin configures a
remote once with ``rclone config``, then points FotoHu at ``remote:path``.

We shell out rather than link a library: rclone has no Python bindings, and its
CLI already handles chunked, resumable, integrity-checked transfers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from ..core.errors import RetryableError, StorageError
from ..core.models import LocalFile, Quota, RemoteFile
from .base import BackendInfo, StorageBackend

log = logging.getLogger(__name__)

TIMEOUT = 3600  # a 2 GB video over a slow uplink still has to fit


class RcloneBackend(StorageBackend):
    info = BackendInfo(
        key="rclone",
        title="Другое облако (через rclone)",
        needs_oauth=False,
        description="Box, Dropbox, pCloud, Яндекс.Диск, WebDAV, S3 и ещё ~70 провайдеров. "
                    "Настраивается командой `rclone config`, боту нужно только имя remote.",
    )

    def __init__(self, account_id: int, root_folder: str, credentials: dict[str, Any],
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(account_id, root_folder, credentials, extra)
        self.binary = self.extra.get("binary") or "rclone"
        self.config_path = self.extra.get("config_path")
        remote = (self.extra.get("remote") or "").strip()
        if remote and not remote.endswith(":") and ":" not in remote:
            remote = f"{remote}:"
        self.remote = remote

    # ------------------------------------------------------------------ process

    def _base_args(self) -> list[str]:
        args = [self.binary, "--use-json-log", "--log-level", "ERROR"]
        if self.config_path:
            args += ["--config", self.config_path]
        return args

    async def _run(self, *args: str, timeout: int = 120) -> str:
        if not shutil.which(self.binary):
            raise StorageError(
                f"rclone binary '{self.binary}' not found on PATH — install rclone "
                "or set RCLONE_BINARY"
            )
        if not self.remote:
            raise StorageError("no rclone remote configured for this storage account")

        cmd = [*self._base_args(), *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RetryableError(f"rclone {args[0]} timed out after {timeout}s") from None

        if proc.returncode != 0:
            message = (stderr or b"").decode(errors="replace").strip()[:400]
            # rclone exit 5 is "temporary error", the documented signal to retry.
            if proc.returncode == 5:
                raise RetryableError(f"rclone {args[0]}: {message}")
            raise StorageError(f"rclone {args[0]} failed ({proc.returncode}): {message}")
        return (stdout or b"").decode(errors="replace")

    def _remote_path(self, path: str) -> str:
        return f"{self.remote}{path.strip('/')}"

    # --------------------------------------------------------------- operations

    async def ensure_folder(self, path: str) -> str:
        await self._run("mkdir", self._remote_path(path))
        return self._remote_path(path)

    async def exists(self, remote_dir: str, filename: str) -> RemoteFile | None:
        try:
            raw = await self._run(
                "lsjson", self._remote_path(f"{remote_dir}/{filename}"), "--stat", "--hash"
            )
        except StorageError:
            return None
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not data:
            return None
        return self._to_remote(data, f"{remote_dir}/{filename}")

    @staticmethod
    def _to_remote(data: dict[str, Any], path: str) -> RemoteFile:
        raw = {k.lower(): v for k, v in (data.get("Hashes") or {}).items()}
        hashes = {}
        for ours, theirs in (("sha256", "sha-256"), ("sha1", "sha-1"), ("md5", "md5")):
            value = raw.get(theirs) or raw.get(ours)
            if value:
                hashes[ours] = value
        return RemoteFile(
            remote_id=data.get("ID") or path,
            path=path,
            size=data.get("Size"),
            hashes=hashes,
        )

    async def upload(self, local: LocalFile, remote_dir: str, filename: str) -> RemoteFile:
        target = self._remote_path(f"{remote_dir}/{filename}")
        await self._run(
            "copyto", str(local.path), target,
            "--retries", "3",
            "--low-level-retries", "10",
            timeout=TIMEOUT,
        )
        stored = await self.exists(remote_dir, filename)
        if stored is None:
            raise StorageError(f"rclone reported success but {target} is not there")
        return stored

    async def quota(self) -> Quota | None:
        try:
            raw = await self._run("about", self.remote, "--json")
        except StorageError:
            return None  # plenty of remotes simply do not implement `about`
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return Quota(total=data.get("total"), used=data.get("used"))

    async def check(self) -> str:
        await self._run("lsjson", self.remote, "--max-depth", "1")
        await self.ensure_folder(self.root_folder)
        return f"rclone {self.remote} ok"
