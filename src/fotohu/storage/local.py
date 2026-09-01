"""Filesystem backend — used by the test-suite and by NAS/rsync setups."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from ..core.models import LocalFile, Quota, RemoteFile
from .base import BackendInfo, StorageBackend


class LocalBackend(StorageBackend):
    info = BackendInfo(
        key="local",
        title="Локальная папка / NAS",
        needs_oauth=False,
        description="Складывает файлы в каталог на диске (или в примонтированную сетевую шару).",
    )

    def __init__(self, account_id: int, root_folder: str, credentials: dict[str, Any],
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(account_id, root_folder, credentials, extra)
        self.base = Path(self.extra.get("base_path") or "./data/storage").expanduser()

    def _abs(self, remote_dir: str, filename: str | None = None) -> Path:
        path = self.base / remote_dir
        return path / filename if filename else path

    async def ensure_folder(self, path: str) -> str:
        target = self._abs(path)
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    async def exists(self, remote_dir: str, filename: str) -> RemoteFile | None:
        target = self._abs(remote_dir, filename)
        if not target.exists():
            return None
        return RemoteFile(
            remote_id=str(target),
            path=f"{remote_dir}/{filename}",
            size=target.stat().st_size,
            hashes={"sha256": hashlib.sha256(target.read_bytes()).hexdigest()},
        )

    async def upload(self, local: LocalFile, remote_dir: str, filename: str) -> RemoteFile:
        await self.ensure_folder(remote_dir)
        target = self._abs(remote_dir, filename)
        shutil.copyfile(local.path, target)
        return RemoteFile(
            remote_id=str(target),
            path=f"{remote_dir}/{filename}",
            size=target.stat().st_size,
            hashes={"sha256": hashlib.sha256(target.read_bytes()).hexdigest()},
        )

    async def quota(self) -> Quota | None:
        self.base.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.base)
        return Quota(total=usage.total, used=usage.used)
