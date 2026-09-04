"""The object graph, assembled once and handed to every handler."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from .config import Config
from .core.models import Platform
from .db import connect, migrate
from .db.repo import Repo
from .i18n import t
from .messengers.base import MessengerAdapter
from .services.members import MemberService
from .services.settings import SettingsService
from .storage.registry import StorageRegistry
from .worker.purger import PurgeWorker
from .worker.uploader import UploadWorker

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    config: Config
    conn: aiosqlite.Connection
    repo: Repo
    settings: SettingsService
    members: MemberService
    storage: StorageRegistry
    adapters: dict[Platform, MessengerAdapter] = field(default_factory=dict)
    uploader: UploadWorker | None = None
    purger: PurgeWorker | None = None

    @property
    def temp_dir(self) -> Path:
        return self.config.temp_dir

    async def text(self, key: str, **kwargs: object) -> str:
        settings = await self.settings.get()
        return t(settings.language, key, **kwargs)

    def register(self, adapter: MessengerAdapter) -> None:
        self.adapters[adapter.platform] = adapter

    async def start_workers(self) -> None:
        self.uploader = UploadWorker(
            repo=self.repo,
            settings_service=self.settings,
            registry=self.storage,
            adapters=self.adapters,
            temp_dir=self.config.temp_dir,
            concurrency=self.config.worker_concurrency,
        )
        self.purger = PurgeWorker(
            repo=self.repo, settings_service=self.settings, adapters=self.adapters
        )
        await self.uploader.start()
        await self.purger.start()

    async def shutdown(self) -> None:
        for worker in (self.uploader, self.purger):
            if worker:
                await worker.stop()
        for adapter in self.adapters.values():
            try:
                await adapter.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("adapter %s did not close cleanly: %s", adapter.platform, exc)
        await self.conn.close()


async def build_context(config: Config) -> AppContext:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.temp_dir.mkdir(parents=True, exist_ok=True)

    conn = await connect(config.db_path)
    applied = await migrate(conn)
    if applied:
        log.info("applied %d migration(s)", applied)

    repo = Repo(conn)
    settings = SettingsService(repo)
    # Let .env seed the interface language on a fresh install.
    if await repo.get_setting("language") is None:
        await settings.set("language", config.language)

    return AppContext(
        config=config,
        conn=conn,
        repo=repo,
        settings=settings,
        members=MemberService(repo, settings),
        storage=StorageRegistry(
            repo,
            config.secret_key,
            rclone_binary=config.rclone_binary,
            rclone_config=config.rclone_config,
        ),
    )
